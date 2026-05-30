"""Support for the Fibaro devices."""

from collections import defaultdict
from collections.abc import Callable, Mapping
import logging
from typing import Any

from .pyfibaro.fibaro_client import (
    FibaroClient,
    FibaroConnectFailed,
)
from .pyfibaro.fibaro_data_helper import find_master_devices, read_rooms
from .pyfibaro.fibaro_device import DeviceModel
from .pyfibaro.fibaro_device_manager import FibaroDeviceManager
from .pyfibaro.fibaro_info import InfoModel
from .pyfibaro.fibaro_scene import SceneModel
from .pyfibaro.fibaro_state_resolver import FibaroEvent

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntry, DeviceInfo
from homeassistant.util import slugify

from .const import CONF_IMPORT_PLUGINS, DOMAIN

# FibaroAuthenticationFailed absent de pyfibaro 0.8.3 — défini localement
class FibaroAuthenticationFailed(Exception):
    """Authentication failed on Fibaro hub."""

type FibaroConfigEntry = ConfigEntry[FibaroController]

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.COVER,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.LOCK,
    Platform.SCENE,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.MEDIA_PLAYER,
]

FIBARO_TYPEMAP = {
    # Sensors
    "com.fibaro.multilevelSensor": Platform.SENSOR,
    "com.fibaro.temperatureSensor": Platform.SENSOR,
    "com.fibaro.lightSensor": Platform.SENSOR,
    "com.fibaro.humiditySensor": Platform.SENSOR,
    "com.fibaro.windSensor": Platform.SENSOR,
    "com.fibaro.rainSensor": Platform.SENSOR,
    "com.fibaro.sensor": Platform.SENSOR,
    "com.fibaro.waterMeter": Platform.SENSOR,
    "com.fibaro.weather": Platform.SENSOR,
    # Binary sensors
    "com.fibaro.doorSensor": Platform.BINARY_SENSOR,
    "com.fibaro.doorWindowSensor": Platform.BINARY_SENSOR,
    "com.fibaro.windowSensor": Platform.BINARY_SENSOR,
    "com.fibaro.FGMS001": Platform.BINARY_SENSOR,
    "com.fibaro.FGDW002": Platform.BINARY_SENSOR,
    "com.fibaro.FGSS001": Platform.BINARY_SENSOR,
    "com.fibaro.FGFS101": Platform.BINARY_SENSOR,
    "com.fibaro.heatDetector": Platform.BINARY_SENSOR,
    "com.fibaro.lifeDangerSensor": Platform.BINARY_SENSOR,
    "com.fibaro.smokeSensor": Platform.BINARY_SENSOR,
    "com.fibaro.floodSensor": Platform.BINARY_SENSOR,
    "com.fibaro.securitySensor": Platform.BINARY_SENSOR,
    "com.fibaro.motionSensor": Platform.BINARY_SENSOR,
    "com.fibaro.binarySensor": Platform.BINARY_SENSOR,
    "com.fibaro.accelerometer": Platform.BINARY_SENSOR,
    # Switches
    "com.fibaro.binarySwitch": Platform.SWITCH,
    "com.fibaro.multilevelSwitch": Platform.SWITCH,
    "com.fibaro.remoteSwitch": Platform.SWITCH,
    "com.fibaro.FGWP101": Platform.SWITCH,
    "com.fibaro.FGWP102": Platform.SWITCH,
    "com.fibaro.FGWOEF011": Platform.SWITCH,
    "com.fibaro.FGWP": Platform.SWITCH,
    # Lights
    "com.fibaro.FGD212": Platform.LIGHT,
    "com.fibaro.colorController": Platform.LIGHT,
    "com.fibaro.FGRGBW441M": Platform.LIGHT,
    "com.fibaro.FGRGBW442CC": Platform.LIGHT,
    # Covers
    "com.fibaro.FGR": Platform.COVER,
    "com.fibaro.FGR223": Platform.COVER,
    "com.fibaro.FGR224": Platform.COVER,
    "com.fibaro.FGRM222": Platform.COVER,
    "com.fibaro.baseShutter": Platform.COVER,
    "com.fibaro.rollerShutter": Platform.COVER,
    "com.fibaro.rollerShutter2": Platform.COVER,
    "com.fibaro.rollerShutter3": Platform.COVER,
    # Climate
    "com.fibaro.hvac": Platform.CLIMATE,
    "com.fibaro.hvacSystem": Platform.CLIMATE,
    "com.fibaro.hvacSystemHeat": Platform.CLIMATE,
    "com.fibaro.setpoint": Platform.CLIMATE,
    "com.fibaro.FGT001": Platform.CLIMATE,
    "com.fibaro.thermostatDanfoss": Platform.CLIMATE,
    # Lock
    "com.fibaro.doorLock": Platform.LOCK,
    # Event
    "com.fibaro.FGPB101": Platform.EVENT,
    "com.fibaro.remoteSceneController": Platform.EVENT,
}


class FibaroController:
    """Initiate Fibaro Controller Class."""

    def __init__(
        self, fibaro_client: FibaroClient, info: InfoModel, import_plugins: bool
    ) -> None:
        """Initialize the Fibaro controller."""
        self._client = fibaro_client
        self._fibaro_info = info

        self._fibaro_device_manager = FibaroDeviceManager(fibaro_client, import_plugins)
        self._room_map = read_rooms(fibaro_client)
        self._device_map: dict[int, DeviceModel]
        self.fibaro_devices: dict[Platform, list[DeviceModel]] = defaultdict(list)
        self._scenes = self._client.read_scenes()
        self.hub_serial = info.serial_number
        self._device_infos: dict[int, DeviceInfo] = {}
        self._read_devices()

    def disconnect(self) -> None:
        """Close push channel."""
        self._fibaro_device_manager.close()

    def register(
        self, device_id: int, callback: Callable[[DeviceModel], None]
    ) -> Callable[[], None]:
        """Register device with a callback for updates."""
        return self._fibaro_device_manager.add_change_listener(device_id, callback)

    def register_event(
        self, device_id: int, callback: Callable[[FibaroEvent], None]
    ) -> Callable[[], None]:
        """Register device with a callback for central scene events."""
        return self._fibaro_device_manager.add_event_listener(device_id, callback)

    def get_children(self, device_id: int) -> list[DeviceModel]:
        """Get a list of child devices."""
        return [
            device
            for device in self._device_map.values()
            if device.parent_fibaro_id == device_id
        ]

    def get_children2(self, device_id: int, endpoint_id: int) -> list[DeviceModel]:
        """Get a list of child devices for the same endpoint."""
        return [
            device
            for device in self._device_map.values()
            if device.parent_fibaro_id == device_id
            and (not device.has_endpoint_id or device.endpoint_id == endpoint_id)
        ]

    def get_siblings(self, device: DeviceModel) -> list[DeviceModel]:
        """Get the siblings of a device."""
        if device.has_endpoint_id:
            return self.get_children2(device.parent_fibaro_id, device.endpoint_id)
        return self.get_children(device.parent_fibaro_id)

    @staticmethod
    def _map_device_to_platform(device: DeviceModel) -> Platform | None:
        """Map device to HA device type."""
        platform: Platform | None = None
        if device.type:
            platform = FIBARO_TYPEMAP.get(device.type)
        if platform is None and device.base_type:
            platform = FIBARO_TYPEMAP.get(device.base_type)

        if platform is None:
            if "setBrightness" in device.actions:
                platform = Platform.LIGHT
            elif "turnOn" in device.actions:
                platform = Platform.SWITCH
            elif "open" in device.actions:
                platform = Platform.COVER
            elif "secure" in device.actions:
                platform = Platform.LOCK
            elif device.has_central_scene_event:
                platform = Platform.EVENT
            elif device.value.has_value and device.value.is_bool_value:
                platform = Platform.BINARY_SENSOR
            elif (
                device.value.has_value
                or "power" in device.properties
                or "energy" in device.properties
            ):
                platform = Platform.SENSOR

        if platform == Platform.SWITCH and device.properties.get("isLight", False):
            platform = Platform.LIGHT
        if platform == Platform.SWITCH and (device.properties.get("deviceRole") == "TvSet"):
            platform = Platform.MEDIA_PLAYER
        return platform

    def _create_device_info(self, main_device: DeviceModel) -> None:
        """Create the device info for a main device."""
        if "zwaveCompany" in main_device.properties:
            manufacturer = main_device.properties.get("zwaveCompany")
        else:
            manufacturer = None

        self._device_infos[main_device.fibaro_id] = DeviceInfo(
            identifiers={(DOMAIN, main_device.fibaro_id)},
            manufacturer=manufacturer,
            name=main_device.name,
            via_device=(DOMAIN, self.hub_serial),
        )

    def get_device_info(self, device: DeviceModel) -> DeviceInfo:
        """Get the device info by fibaro device id."""
        if device.fibaro_id in self._device_infos:
            return self._device_infos[device.fibaro_id]
        if device.parent_fibaro_id in self._device_infos:
            return self._device_infos[device.parent_fibaro_id]
        return DeviceInfo(identifiers={(DOMAIN, self.hub_serial)})

    def get_all_device_identifiers(self) -> list[set[tuple[str, str]]]:
        """Get all identifiers of fibaro integration."""
        return [device["identifiers"] for device in self._device_infos.values()]

    def get_room_name(self, room_id: int) -> str | None:
        """Get the room name by room id."""
        return self._room_map.get(room_id)

    def read_scenes(self) -> list[SceneModel]:
        """Return list of scenes."""
        return self._scenes

    def get_all_devices(self) -> list[DeviceModel]:
        """Return list of all fibaro devices."""
        return self._fibaro_device_manager.get_devices()

    def read_fibaro_info(self) -> InfoModel:
        """Return the general info about the hub."""
        return self._fibaro_info

    def get_frontend_url(self) -> str:
        """Return the url to the Fibaro hub web UI."""
        return self._client.frontend_url()

    def _read_devices(self) -> None:
        """Read and process the device list."""
        devices = self._fibaro_device_manager.get_devices()

        for main_device in find_master_devices(devices):
            self._create_device_info(main_device)

        self._device_map = {}
        last_climate_parent = None
        last_endpoint = None
        for device in devices:
            try:
                device.fibaro_controller = self
                room_name = self.get_room_name(device.room_id)
                if not room_name:
                    room_name = "Unknown"
                device.room_name = room_name
                device.friendly_name = f"{room_name} {device.name}"
                device.ha_id = (
                    f"{slugify(room_name)}_{slugify(device.name)}_{device.fibaro_id}"
                )

                platform = self._map_device_to_platform(device)
                if platform is None:
                    continue
                device.unique_id_str = f"{slugify(self.hub_serial)}.{device.fibaro_id}"
                self._device_map[device.fibaro_id] = device
                _LOGGER.debug(
                    "%s (%s, %s) -> %s %s",
                    device.ha_id,
                    device.type,
                    device.base_type,
                    platform,
                    str(device),
                )
                if platform != Platform.CLIMATE:
                    self.fibaro_devices[platform].append(device)
                    continue

                if device.has_endpoint_id:
                    _LOGGER.debug(
                        "climate device: %s, endPointId: %s",
                        device.ha_id,
                        device.endpoint_id,
                    )
                else:
                    _LOGGER.debug("climate device: %s, no endPointId", device.ha_id)

                if (
                    last_climate_parent != device.parent_fibaro_id
                    or (device.has_endpoint_id and last_endpoint != device.endpoint_id)
                    or device.parent_fibaro_id == 0
                ):
                    _LOGGER.debug("Handle separately")
                    self.fibaro_devices[platform].append(device)
                    last_climate_parent = device.parent_fibaro_id
                    last_endpoint = device.endpoint_id
                else:
                    _LOGGER.debug("not handling separately")
            except (KeyError, ValueError):
                pass


def connect_fibaro_client(data: Mapping[str, Any]) -> tuple[InfoModel, FibaroClient]:
    """Connect to the fibaro hub and read some basic data."""
    client = FibaroClient(data[CONF_URL])
    info = client.connect_with_credentials(data[CONF_USERNAME], data[CONF_PASSWORD])
    return (info, client)


def init_controller(data: Mapping[str, Any]) -> FibaroController:
    """Connect to the fibaro hub and init the controller."""
    info, client = connect_fibaro_client(data)
    return FibaroController(client, info, data[CONF_IMPORT_PLUGINS])


async def async_setup_entry(hass: HomeAssistant, entry: FibaroConfigEntry) -> bool:
    """Set up the Fibaro Component."""
    try:
        controller = await hass.async_add_executor_job(init_controller, entry.data)
    except FibaroConnectFailed as connect_ex:
        raise ConfigEntryNotReady(
            f"Could not connect to controller at {entry.data[CONF_URL]}"
        ) from connect_ex
    except FibaroAuthenticationFailed as auth_ex:
        raise ConfigEntryAuthFailed from auth_ex

    entry.runtime_data = controller

    fibaro_info = controller.read_fibaro_info()
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, controller.hub_serial)},
        serial_number=controller.hub_serial,
        manufacturer=fibaro_info.manufacturer_name,
        name=fibaro_info.hc_name,
        model=fibaro_info.model_name,
        sw_version=fibaro_info.current_version,
        configuration_url=controller.get_frontend_url(),
        connections={(dr.CONNECTION_NETWORK_MAC, fibaro_info.mac_address)},
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: FibaroConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Shutting down Fibaro connection")
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    entry.runtime_data.disconnect()
    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: FibaroConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Remove a device entry from fibaro integration."""
    controller = config_entry.runtime_data
    for identifiers in controller.get_all_device_identifiers():
        if device_entry.identifiers == identifiers:
            return False
    return True
