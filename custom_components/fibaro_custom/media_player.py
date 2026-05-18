"""Support for Fibaro switches."""
import logging
from typing import Any

from .pyfibaro.fibaro_device import DeviceModel

from homeassistant.components.media_player import (
    ENTITY_ID_FORMAT,
    MediaPlayerDeviceClass,
    MediaPlayerState,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaType,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FibaroConfigEntry
from .entity import FibaroEntity

_LOGGER = logging.getLogger(__name__)

SOURCES_SamsungTV = {"TV": "uiTv", "HDMI1": "uiHdmi1", "HDMI2": "uiHdmi2", "HDMI3": "uiHdmi3", "HDMI4": "uiHdmi4"}

SUPPORT_SamsungTV = (
    MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PLAY_MEDIA
)

SUPPORT_X96Mini = (
    MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.PLAY
)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Fibaro MediaPlayer."""
    controller: FibaroController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [FibaroMediaPlayer(device) for device in controller.fibaro_devices[Platform.MEDIA_PLAYER]],
        True,
    )


class FibaroMediaPlayer(FibaroDevice, MediaPlayerEntity):
    """Representation of a Fibaro MediaPlayer."""

    _attr_source_list: list[str]
    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.TV

    def __init__(self, fibaro_device: DeviceModel) -> None:
        """Initialize the Fibaro device."""
        super().__init__(fibaro_device)
        self.entity_id = ENTITY_ID_FORMAT.format(self.ha_id)

        if self.fibaro_device.name == "SamsungTV":
            self._attr_supported_features = SUPPORT_SamsungTV
            self._attr_source_list = list(SOURCES_SamsungTV)
        elif self.fibaro_device.name == "X96Mini":
            self._attr_supported_features = SUPPORT_X96Mini

        # Assume that the TV is in Play mode
        self._playing: bool = True

    @property
    def name(self):
        """Return the name of the media player."""
        return self.fibaro_device.name
    
    @property
    def state(self):
        """Return the state of the player"""
        state = self.fibaro_device.state
        if not state.has_value or state.str_value().lower() == "unknown":
            return None
        if state.str_value().lower() == "false":
            return MediaPlayerState.OFF
        if state.str_value().lower() == "true":
            return MediaPlayerState.ON
        return state.str_value().lower()

    def async_turn_on(self) -> None:
        """Turn the media player on."""
        self.call_turn_on()

    def async_turn_off(self) -> None:
        """Turn off media player."""
        self.call_turn_off()

    def async_volume_up(self) -> None:
        """Volume up the media player."""
        self.callAction("uiVolumeUp")

    def async_volume_down(self) -> None:
        """Volume down media player."""
        self.callAction("uiVolumeDown")

    def async_mute_volume(self, mute: bool) -> None:
        """Send mute command."""
        self.callAction("uiMute")

    def async_media_play_pause(self) -> None:
        """Simulate play pause media player."""
        if self._playing:
            self.callAction("uiPause")
        else:
            self.callAction("uiPlay")

    def async_media_play(self) -> None:
        """Send play command."""
        self._playing = True
        self.callAction("uiPlay")

    def async_media_pause(self) -> None:
        """Send media pause command to media player."""
        self._playing = False
        self.callAction("uiPause")

    def async_media_next_track(self) -> None:
        """Send next track command."""
        self.callAction("uiChUp")

    def async_media_previous_track(self) -> None:
        """Send the previous track command."""
        self.callAction("uiChDown")

    def async_select_source(self, source: str) -> None:
        """Select input source."""
        if source in SOURCES_SamsungTV:
            self.callAction(SOURCES_SamsungTV[source])
            return

    def async_play_media(self, media_type: MediaType | str, media_id: str, **kwargs: Any) -> None:
        """Support changing a channel."""
        if media_type != MediaType.CHANNEL:
            _LOGGER.error(f"Unsupported media type: {media_type}/{MediaType.CHANNEL}")
            return

        # media_id should only be a channel number
        if not media_id.isnumeric():
            _LOGGER.error(f"Channel must be numeric: {media_id}")
            return
        
        _LOGGER.debug(f"Change channel: {media_id}")

        self.callAction(f"uiNum{media_id}")

