"""Support for the Fibaro devices."""

from collections import defaultdict
from collections.abc import Callable, Mapping
import logging
from typing import Any

from pyfibaro.fibaro_client import (
    FibaroAuthenticationFailed,
    FibaroClient,
    FibaroConnectFailed,
)
from pyfibaro.fibaro_data_helper import find_master_devices, read_rooms
from pyfibaro.fibaro_device import DeviceModel
from pyfibaro.fibaro_device_manager import FibaroDeviceManager
from pyfibaro.fibaro_info import InfoModel
from pyfibaro.fibaro_scene import SceneModel
from pyfibaro.fibaro_state_resolver import FibaroEvent

from .pyfibaro.fibaro_room import RoomModel

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ARMED,
    ATTR_BATTERY_LEVEL,
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntry, DeviceInfo
from homeassistant.util import slugify

from .const import CONF_IMPORT_PLUGINS, DOMAIN

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
    "com.fibaro.multilevelSensor": Platform.SENSOR,
    "com.fibaro.binarySwitch": Platform.SWITCH,
    "com.fibaro.multilevelSwitch": Platform.SWITCH,
    "com.fibaro.FGD212": Platform.LIGHT,
    "com.fibaro.FGR": Platform.COVER,
    "com.fibaro.doorSensor": Platform.BINARY_SENSOR,
    "com.fibaro.doorWindowSensor": Platform.BINARY_SENSOR,
    "com.fibaro.FGMS001": Platform.BINARY_SENSOR,
    "com.fibaro.heatDetector": Platform.BINARY_SENSOR,
    "com.fibaro.lifeDangerSensor": Platform.BINARY_SENSOR,
    "com.fibaro.smokeSensor": Platform.BINARY_SENSOR,
    "com.fibaro.remoteSwitch": Platform.SWITCH,
    "com.fibaro.sensor": Platform.SENSOR,
    "com.fibaro.waterMeter": Platform.SENSOR,
    "com.fibaro.colorController": Platform.LIGHT,
    "com.fibaro.securitySensor": Platform.BINARY_SENSOR,
    "com.fibaro.hvac": Platform.CLIMATE,
    "com.fibaro.hvacSystem": Platform.CLIMATE,
    "com.fibaro.setpoint": Platform.CLIMATE,
    "com.fibaro.FGT001": Platform.CLIMATE,
    "com.fibaro.thermostatDanfoss": Platform.CLIMATE,
    "com.fibaro.doorLock": Platform.LOCK,
    "com.fibaro.binarySensor": Platform.BINARY_SENSOR,
    "com.fibaro.accelerometer": Platform.BINARY_SENSOR,
}


class FibaroController:
    """Initiate Fibaro Controller Class."""

    def __init__(
        self, fibaro_client: FibaroClient, info: InfoModel, import_plugins: bool
    ) -> None:
        """Initialize the Fibaro controller."""
        self._client = fibaro_client
        self._fibaro_info = info

        # The fibaro device manager exposes higher level API to access fibaro devices
        self._fibaro_device_manager = FibaroDeviceManager(fibaro_client, import_plugins)
        # Mapping roomId to room object
        self._room_map = read_rooms(fibaro_client)
        self._device_map: dict[int, DeviceModel]  # Mapping deviceId to device object
        self.fibaro_devices: dict[Platform, list[DeviceModel]] = defaultdict(
            list
        )  # List of devices by entity platform
        # All scenes
        self._scenes = self._client.read_scenes()
        # Unique serial number of the hub
        self.hub_serial = info.serial_number
        # Device infos by fibaro device id
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
        """Register device with a callback for central scene events.

        The callback receives one parameter with the event.
        """
        return self._fibaro_device_manager.add_event_listener(device_id, callback)

    def get_children(self, device_id: int) -> list[DeviceModel]:
        """Get a list of child devices."""
        return [
            device
            for device in self._device_map.values()
            if device.parent_fibaro_id == device_id
        ]def get_children2(self, device_id: int, endpoint_id: int) -> list[DeviceModel]:
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
        # Use our lookup table to identify device type
        platform: Platform | None = None
        if device.type:
            platform = FIBARO_TYPEMAP.get(device.type)
        if platform is None and device.base_type:
            platform = FIBARO_TYPEMAP.get(device.base_type)

        # We can also identify device type by its capabilities
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

        # Switches that control lights should show up as lights
        if platform == Platform.SWITCH and device.properties.get("isLight", False):
            platform = Platform.LIGHT
        # Switches that control TV should show up as Remotes
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
                # We group climate devices into groups with the same
                # endPointID belonging to the same parent device.
                if device.has_endpoint_id:
                    _LOGGER.debug(
                        "climate device: %s, endPointId: %s",
                        device.ha_id,
                        device.endpoint_id,
                    )
                else:
                    _LOGGER.debug("climate device: %s, no endPointId", device.ha_id)
                # If a sibling of this device has been added, skip this one
                # otherwise add the first visible device in the group
                # which is a hack, but solves a problem with FGT having
                # hidden compatibility devices before the real device
                # Second hack is for quickapps which have parent id 0 and no children
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
            except KeyError, ValueError:
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
    """Set up the Fibaro Component.

    The unique id of the config entry is the serial number of the home center.
    """
    try:
        controller = await hass.async_add_executor_job(init_controller, entry.data)
    except FibaroConnectFailed as connect_ex:
        raise ConfigEntryNotReady(
            f"Could not connect to controller at {entry.data[CONF_URL]}"
        ) from connect_ex
    except FibaroAuthenticationFailed as auth_ex:
        raise ConfigEntryAuthFailed from auth_ex

    entry.runtime_data = controller

    # register the hub device info separately as the hub has sometimes no entities
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
    """Remove a device entry from fibaro integration.

    Only removing devices which are not present anymore are eligible to be removed.
    """
    controller = config_entry.runtime_data
    for identifiers in controller.get_all_device_identifiers():
        if device_entry.identifiers == identifiers:
            # Fibaro device is still served by the controller,
            # do not allow to remove the device entry
            return False

    return True

class FibaroDevice(Entity):
    """Representation of a Fibaro device entity."""

    _attr_should_poll = False

    def __init__(self, fibaro_device: DeviceModel) -> None:
        """Initialize the device."""
        self.fibaro_device = fibaro_device
        self.controller = fibaro_device.fibaro_controller
        self.ha_id = fibaro_device.ha_id
        self._attr_name = fibaro_device.friendly_name
        self._attr_unique_id = fibaro_device.unique_id_str

        self._attr_device_info = self.controller.get_device_info(fibaro_device)
        # propagate hidden attribute set in fibaro home center to HA
        if not fibaro_device.visible:
            self._attr_entity_registry_visible_default = False

    async def async_added_to_hass(self) -> None:
        """Call when entity is added to hass."""
        self.controller.register(self.fibaro_device.fibaro_id, self._update_callback)

    def _update_callback(self) -> None:
        """Update the state."""
        self.schedule_update_ha_state(True)

    @property
    def level(self) -> int | None:
        """Get the level of Fibaro device."""
        if self.fibaro_device.value.has_value:
            return self.fibaro_device.value.int_value()
        return None

    @property
    def level2(self) -> int | None:
        """Get the tilt level of Fibaro device."""
        if self.fibaro_device.value_2.has_value:
            return self.fibaro_device.value_2.int_value()
        return None

    def dont_know_message(self, cmd: str) -> None:
        """Make a warning in case we don't know how to perform an action."""
        _LOGGER.warning(
            "Not sure how to %s: %s (available actions: %s)",
            cmd,
            str(self.ha_id),
            str(self.fibaro_device.actions),
        )

    def dont_know_callAction(self, callAction) -> None:
        """Make a warning in case we don't know how to perform a callback function."""
        _LOGGER.warning(
            "Not sure how to call function: %s (available uiCallbacks: %s)",
            str(self.ha_id),
            str(self.fibaro_device.uiCallbacks),
        )

    def set_level(self, level: int) -> None:
        """Set the level of Fibaro device."""
        self.action("setValue", level)
        if self.fibaro_device.value.has_value:
            self.fibaro_device.properties["value"] = level
        if self.fibaro_device.has_brightness:
            self.fibaro_device.properties["brightness"] = level

    def set_level2(self, level: int) -> None:
        """Set the level2 of Fibaro device."""
        self.action("setValue2", level)
        if self.fibaro_device.value_2.has_value:
            self.fibaro_device.properties["value2"] = level

    def call_turn_on(self) -> None:
        """Turn on the Fibaro device."""
        self.action("turnOn")

    def call_turn_off(self) -> None:
        """Turn off the Fibaro device."""
        self.action("turnOff")

    def call_set_color(self, red: int, green: int, blue: int, white: int) -> None:
        """Set the color of Fibaro device."""
        red = int(max(0, min(255, red)))
        green = int(max(0, min(255, green)))
        blue = int(max(0, min(255, blue)))
        white = int(max(0, min(255, white)))
        color_str = f"{red},{green},{blue},{white}"
        self.fibaro_device.properties["color"] = color_str
        self.action("setColor", str(red), str(green), str(blue), str(white))

    def action(self, cmd: str, *args: Any) -> None:
        """Perform an action on the Fibaro HC."""
        if cmd in self.fibaro_device.actions:
            self.fibaro_device.execute_action(cmd, args)
            _LOGGER.debug("-> %s.%s%s called", str(self.ha_id), str(cmd), str(args))
        else:
            self.dont_know_message(cmd)

    def callAction(self, cmd, *args) -> None:
        """Perform an action on the Fibaro HC."""
        self.fibaro_device.execute_callAction(cmd, args)
        _LOGGER.debug("-> %s.%s%s called", str(self.ha_id), str(cmd), str(args))
        
    @property
    def current_binary_state(self) -> bool:
        """Return the current binary state."""
        return self.fibaro_device.value.bool_value(False)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return the state attributes of the device."""
        attr = {"fibaro_id": self.fibaro_device.fibaro_id}

        if self.fibaro_device.has_battery_level:
            attr[ATTR_BATTERY_LEVEL] = self.fibaro_device.battery_level
        if self.fibaro_device.has_armed:
            attr[ATTR_ARMED] = self.fibaro_device.armed

        return attr

    def update(self) -> None:
        """Update the available state of the entity."""
        if self.fibaro_device.has_dead:
            self._attr_available = not self.fibaro_device.dead


class FibaroConnectFailed(HomeAssistantError):
    """Error to indicate we cannot connect to fibaro home center."""


class FibaroAuthFailed(HomeAssistantError):
    """Error to indicate that authentication failed on fibaro home center."""
