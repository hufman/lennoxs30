"""Support for Lennoxs30 cloud api"""
import asyncio
from asyncio.locks import Event, Lock
import logging

from lennoxs30api.s30exception import EC_CONFIG_TIMEOUT

from lennoxs30api import (
    EC_HTTP_ERR,
    EC_LOGIN,
    EC_SUBSCRIBE,
    EC_UNAUTHORIZED,
    S30Exception,
    s30api_async,
)
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

DOMAIN = "lennoxs30"
DOMAIN_STATE = "lennoxs30.state"

DS_CONNECTING = "Connecting"
DS_DISCONNECTED = "Disconnected"
DS_LOGIN_FAILED = "Login Failed"
DS_CONNECTED = "Connected"
DS_RETRY_WAIT = "Waiting to Retry"
DS_FAILED = "Failed"

from homeassistant.const import (
    CONF_EMAIL,
    CONF_HOSTS,
    CONF_IP_ADDRESS,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    EVENT_HOMEASSISTANT_STOP,
)

CONF_FAST_POLL_INTERVAL = "fast_scan_interval"
CONF_ALLERGEN_DEFENDER_SWITCH = "allergen_defender_switch"
CONF_APP_ID = "app_id"
CONF_INIT_WAIT_TIME = "init_wait_time"
CONF_CREATE_SENSORS = "create_sensors"
CONF_CREATE_INVERTER_POWER = "create_inverter_power"
CONF_PROTOCOL = "protocol"
DEFAULT_POLL_INTERVAL: int = 10
DEFAULT_LOCAL_POLL_INTERVAL: int = 1
DEFAULT_FAST_POLL_INTERVAL: float = 0.75
MAX_ERRORS = 5
RETRY_INTERVAL_SECONDS = 60

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_EMAIL): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
                vol.Optional(CONF_HOSTS, default="Cloud"): str,
                vol.Optional(CONF_SCAN_INTERVAL): cv.positive_int,
                vol.Optional(
                    CONF_FAST_POLL_INTERVAL, default=DEFAULT_FAST_POLL_INTERVAL
                ): cv.positive_float,
                vol.Optional(CONF_ALLERGEN_DEFENDER_SWITCH, default=False): cv.boolean,
                vol.Optional(CONF_APP_ID): cv.string,
                vol.Optional(CONF_INIT_WAIT_TIME, default=30): cv.positive_int,
                vol.Optional(CONF_CREATE_SENSORS, default=False): cv.boolean,
                vol.Optional(CONF_CREATE_INVERTER_POWER, default=False): cv.boolean,
                vol.Optional(CONF_PROTOCOL, default="https"): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    email = config.get(DOMAIN).get(CONF_EMAIL)
    password = config.get(DOMAIN).get(CONF_PASSWORD)
    conf_hosts: str = config.get(DOMAIN).get(CONF_HOSTS)
    host_list = []
    if conf_hosts == "Cloud":
        conf_hosts = None
        host_list.append(None)
    else:
        host_list = conf_hosts.split(",")

    t = config.get(DOMAIN).get(CONF_SCAN_INTERVAL)
    if t != None and t > 0:
        poll_interval = t
    else:
        if conf_hosts == None:
            poll_interval = DEFAULT_POLL_INTERVAL
        else:
            poll_interval = DEFAULT_LOCAL_POLL_INTERVAL

    t = config.get(DOMAIN).get(CONF_FAST_POLL_INTERVAL)
    if t != None and t > 0.2:
        fast_poll_interval = t
    else:
        fast_poll_interval = DEFAULT_FAST_POLL_INTERVAL

    allergenDefenderSwitch = config.get(DOMAIN).get(CONF_ALLERGEN_DEFENDER_SWITCH)
    app_id = config.get(DOMAIN).get(CONF_APP_ID)
    conf_init_wait_time = config.get(DOMAIN).get(CONF_INIT_WAIT_TIME)
    create_sensors = config.get(DOMAIN).get(CONF_CREATE_SENSORS)
    create_inverter_power = config.get(DOMAIN).get(CONF_CREATE_INVERTER_POWER)
    conf_protocol = config.get(DOMAIN).get(CONF_PROTOCOL)

    index = 0

    for host_name in host_list:
        _LOGGER.debug(
            f"async_setup starting host [{host_name}] scan_interval [{poll_interval}] fast_scan_interval[{fast_poll_interval}] app_id [{app_id}] config_init_wait_time [{conf_init_wait_time}] create_sensors [{create_sensors}] create_inverter_power [{create_inverter_power}]"
        )

        manager = Manager(
            hass=hass,
            config=config,
            email=email,
            password=password,
            poll_interval=poll_interval,
            fast_poll_interval=fast_poll_interval,
            allergenDefenderSwitch=allergenDefenderSwitch,
            app_id=app_id,
            conf_init_wait_time=conf_init_wait_time,
            ip_address=host_name,
            create_sensors=create_sensors,
            create_inverter_power=create_inverter_power,
            protocol=conf_protocol,
            index=index,
        )
        try:
            listener = hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, manager.async_shutdown
            )
            await manager.s30_initalize()
        except S30Exception as e:
            if e.error_code == EC_LOGIN:
                # TODO: encapsulate in manager class
                manager.updateState(DS_LOGIN_FAILED)
                raise HomeAssistantError(
                    f"Lennox30 unable to login host [{host_name}] - please check credentials and restart Home Assistant"
                )
            elif e.error_code == EC_CONFIG_TIMEOUT:
                _LOGGER.warning("async_setup: " + e.message)
                _LOGGER.info("connection will be retried in 1 minute")
                asyncio.create_task(manager.initialize_retry_task())
            else:
                _LOGGER.error("async_setup unexpected error " + e.message)
                _LOGGER.info("connection will be retried in 1 minute")
                asyncio.create_task(manager.initialize_retry_task())
        _LOGGER.debug(f"async_setup complete host [{host_name}]")
        index = index + 1
    return True


class Manager(object):
    def __init__(
        self,
        hass: HomeAssistant,
        config: ConfigType,
        email: str,
        password: str,
        poll_interval: int,
        fast_poll_interval: float,
        allergenDefenderSwitch: bool,
        app_id: str,
        conf_init_wait_time: int,
        ip_address: str,
        create_sensors: bool,
        create_inverter_power: bool,
        protocol: str,
        index: int = 0,
    ):
        self._reinitialize: bool = False
        self._err_cnt: int = 0
        self._mp_wakeup_event: Event = Event()
        self._climate_entities_initialized: bool = False
        self._hass: HomeAssistant = hass
        self._config: ConfigType = config
        self._poll_interval: int = poll_interval
        self._fast_poll_interval: float = fast_poll_interval
        self._protocol = protocol
        self._ip_address = ip_address
        self._api: s30api_async = s30api_async(
            email, password, app_id, ip_address=ip_address, protocol=self._protocol
        )
        self._shutdown = False
        self._retrieve_task = None
        self._allergenDefenderSwitch = allergenDefenderSwitch
        self._createSensors: bool = create_sensors
        self._create_inverter_power: bool = create_inverter_power
        self._conf_init_wait_time = conf_init_wait_time
        self._is_metric: bool = hass.config.units.is_metric
        # For backwards compatibility
        if index == 0:
            self.connection_state = DOMAIN_STATE
        else:
            self.connection_state = "lennoxs30.conn_" + self._ip_address.replace(
                ".", "_"
            ).replace(":", "_")

    async def async_shutdown(self, event: Event) -> None:
        _LOGGER.debug(f"async_shutdown started host [{self._ip_address}]")
        self._shutdown = True
        if self._retrieve_task != None:
            self._mp_wakeup_event.set()
            await self._retrieve_task
        await self._api.shutdown()
        _LOGGER.debug(f"async_shutdown complete [{self._ip_address}]")

    def updateState(self, state: int) -> None:
        self._hass.states.async_set(
            self.connection_state, state, self.getMetricsList(), force_update=True
        )

    def getMetricsList(self):
        list = self._api.metrics.getMetricList()
        # TODO these are at the individual S30 level, when we have a device object we should move this there
        systems = self._api.getSystems()
        if len(systems) > 0:
            system: s30api_async.lennox_system = self._api.getSystems()[0]
            if system != None:
                list["sysUpTime"] = system.sysUpTime
                list["diagLevel"] = system.diagLevel
                list["softwareVersion"] = system.softwareVersion
                list["hostname"] = self._ip_address
        return list

    async def s30_initalize(self):
        self.updateState(DS_CONNECTING)
        await self.connect_subscribe()
        await self.configuration_initialization()
        # Launch the message pump loop
        self._retrieve_task = asyncio.create_task(self.messagePump_task())
        # Only add entities the first time, on reconnect we do not need to add them again
        if self._climate_entities_initialized == False:
            self._hass.helpers.discovery.load_platform(
                "climate", DOMAIN, self, self._config
            )
            self._hass.helpers.discovery.load_platform(
                "sensor", DOMAIN, self, self._config
            )
            self._hass.helpers.discovery.load_platform(
                "switch", DOMAIN, self, self._config
            )

            self._climate_entities_initialized = True
        self.updateState(DS_CONNECTED)

    async def initialize_retry_task(self):
        while True:
            self.updateState(DS_RETRY_WAIT)
            await asyncio.sleep(RETRY_INTERVAL_SECONDS)
            self.updateState(DS_CONNECTING)
            try:
                await self.s30_initalize()
                self.updateState(DS_CONNECTED)
                return

            except S30Exception as e:
                if e.error_code == EC_LOGIN:
                    # TODO: encapsulate in manager class
                    self.updateState(DS_LOGIN_FAILED)
                    _LOGGER.error(
                        f"initialize_retry_task host [{self._ip_address}] - "
                        + e.message
                    )
                    return
                elif e.error_code == EC_CONFIG_TIMEOUT:
                    _LOGGER.warning(
                        f"async_setup: host [{self._ip_address}] " + e.message
                    )
                    _LOGGER.info(
                        f"connection host [{self._ip_address}] will be retried in 1 minute"
                    )
                else:
                    _LOGGER.error(
                        f"async_setup host [{self._ip_address}] unexpected error "
                        + e.message
                    )
                    _LOGGER.info(
                        f"async setup host [{self._ip_address}] will be retried in 1 minute"
                    )

    async def configuration_initialization(self) -> None:
        # Wait for zones to appear on each system
        sytemsWithZones = 0
        loops: int = 0
        numOfSystems = len(self._api.getSystems())
        while sytemsWithZones < numOfSystems and loops < self._conf_init_wait_time:
            _LOGGER.debug(
                f"__init__:async_setup waiting for zone config to arrive host [{self._ip_address}]  numSystems ["
                + str(numOfSystems)
                + "] sytemsWithZones ["
                + str(sytemsWithZones)
                + "]"
            )
            sytemsWithZones = 0
            await asyncio.sleep(1.0)
            await self.messagePump()
            for lsystem in self._api.getSystems():
                # Issue #33 - system configuration isn't complete until we've received the name from Lennox.
                if lsystem.config_complete() == False:
                    continue
                numZones = len(lsystem.getZoneList())
                _LOGGER.debug(
                    f"__init__:async_setup host [{self._ip_address}] wait for zones system ["
                    + lsystem.sysId
                    + "] numZone ["
                    + str(numZones)
                    + "]"
                )
                if numZones > 0:
                    sytemsWithZones += 1
            loops += 1
        if sytemsWithZones < numOfSystems:
            raise S30Exception(
                "Timeout waiting for configuration data from Lennox - this sometimes happens, the connection will be automatically retried.  Consult the readme for more details",
                EC_CONFIG_TIMEOUT,
                1,
            )

    async def connect_subscribe(self):
        await self._api.serverConnect()

        for lsystem in self._api.getSystems():
            await self._api.subscribe(lsystem)

    async def reinitialize_task(self) -> None:
        while True:
            try:
                self.updateState(DS_CONNECTING)
                _LOGGER.debug(
                    f"reinitialize_task host [{self._ip_address}] - trying reconnect"
                )
                await self.connect_subscribe()
                self.updateState(DS_CONNECTED)
                break
            except S30Exception as e:
                _LOGGER.error(
                    f"reinitialize_task host [{self._ip_address}] : " + str(e)
                )
                if e.error_code == EC_LOGIN:
                    raise HomeAssistantError(
                        f"Lennox30 unable to login host [{self._ip_address}]  - please check credentials and restart Home Assistant"
                    )
            self.updateState(DS_RETRY_WAIT)
            await asyncio.sleep(RETRY_INTERVAL_SECONDS)

        _LOGGER.debug(
            f"reinitialize_task host [{self._ip_address}] - reconnect successful"
        )
        asyncio.create_task(self.messagePump_task())

    async def event_wait_mp_wakeup(self, timeout: float) -> bool:
        # suppress TimeoutError because we'll return False in case of timeout
        try:
            await asyncio.wait_for(self._mp_wakeup_event.wait(), timeout)
        except asyncio.TimeoutError as e:
            return False
        return self._mp_wakeup_event.is_set()

    async def messagePump_task(self) -> None:
        await asyncio.sleep(self._poll_interval)
        self._reinitialize = False
        self._err_cnt = 0
        fast_polling: bool = False
        fast_polling_cd: int = 0
        received = False
        while self._reinitialize == False:
            try:
                received = await self.messagePump()
            except Exception as e:
                _LOGGER.error(
                    f"messagePump_task host [{self._ip_address}] unexpected exception:"
                    + str(e)
                )
            if fast_polling == True:
                fast_polling_cd = fast_polling_cd - 1
                if fast_polling_cd <= 0:
                    fast_polling = False

            if self._shutdown == True:
                break

            if not received:
                if fast_polling == True:
                    res = await asyncio.sleep(
                        min(self._fast_poll_interval, self._poll_interval)
                    )
                else:
                    res = await self.event_wait_mp_wakeup(self._poll_interval)
                    if res == True:
                        self._mp_wakeup_event.clear()
                        fast_polling = True
                        fast_polling_cd = 10

        if self._shutdown == True:
            _LOGGER.debug(
                f"messagePump_task host [{self._ip_address}] is exiting to shutdown"
            )
            return
        elif self._reinitialize == True:
            self.updateState(DS_DISCONNECTED)
            asyncio.create_task(self.reinitialize_task())
            _LOGGER.debug(
                f"messagePump_task host [{self._ip_address}] is exiting - to enter retries"
            )
        else:
            _LOGGER.debug(
                f"messagePump_task host [{self._ip_address}] is exiting - and this should not happen"
            )

    async def messagePump(self) -> bool:
        bErr = False
        received = False
        try:
            _LOGGER.debug(f"messagePump_task host [{self._ip_address}] running")
            received = await self._api.messagePump()
            self.updateState(DS_CONNECTED)
        except S30Exception as e:
            self._err_cnt += 1
            # This should mean we have been logged out and need to start the login process
            if e.error_code == EC_UNAUTHORIZED:
                _LOGGER.debug(
                    f"messagePump_task host [{self._ip_address}] - unauthorized - trying to relogin"
                )
                self._reinitialize = True
            # If its an HTTP error, we will not log an error, just and info message, unless
            # this exeeeds the max consecutive error count
            elif e.error_code == EC_HTTP_ERR and self._err_cnt < MAX_ERRORS:
                _LOGGER.debug(
                    f"messagePump_task - host [{self._ip_address}] S30Exception "
                    + str(e)
                )
            else:
                _LOGGER.error(
                    f"messagePump_task - host [{self._ip_address}] S30Exception "
                    + str(e)
                )
            bErr = True
        except Exception as e:
            _LOGGER.error(
                f"messagePump_task host [{self._ip_address}] - Exception " + str(e)
            )
            self._err_cnt += 1
            bErr = True
        # Keep retrying retrive up until we get this number of errors in a row, at which point will try to reconnect
        if self._err_cnt > MAX_ERRORS:
            self._reinitialize = True
        if bErr is False:
            self._err_cnt = 0
        return received
