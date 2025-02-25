"""Sensor platform for battery_notes."""
from __future__ import annotations

import logging
from datetime import datetime
from dataclasses import dataclass
import voluptuous as vol

from homeassistant.components.sensor import (
    PLATFORM_SCHEMA,
    SensorDeviceClass,
    SensorStateClass,
    SensorEntity,
    SensorEntityDescription,
    RestoreSensor,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import HomeAssistant, callback, Event
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
    async_track_entity_registry_updated_event,
)
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
)
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.typing import EventType

from homeassistant.const import (
    CONF_NAME,
    CONF_DEVICE_ID,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    PERCENTAGE,
)

from .const import (
    DOMAIN,
    PLATFORMS,
    CONF_BATTERY_TYPE,
    CONF_BATTERY_QUANTITY,
    DATA,
    LAST_REPLACED,
    DOMAIN_CONFIG,
    CONF_ENABLE_REPLACED,
    CONF_HIDE_BATTERY,
    CONF_ROUND_BATTERY,
    ATTR_BATTERY_QUANTITY,
    ATTR_BATTERY_TYPE,
    ATTR_BATTERY_TYPE_AND_QUANTITY,
    ATTR_BATTERY_LAST_REPLACED,
    ATTR_BATTERY_LOW,
    ATTR_BATTERY_LOW_THRESHOLD,
)

from .common import isfloat
from .device import BatteryNotesDevice
from .coordinator import BatteryNotesCoordinator

from .entity import (
    BatteryNotesEntityDescription,
)


@dataclass
class BatteryNotesSensorEntityDescription(
    BatteryNotesEntityDescription,
    SensorEntityDescription,
):
    """Describes Battery Notes sensor entity."""

    unique_id_suffix: str


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME): cv.string,
        vol.Required(CONF_DEVICE_ID): cv.string,
        vol.Required(CONF_BATTERY_TYPE): cv.string,
        vol.Required(CONF_BATTERY_QUANTITY): cv.positive_int,
    }
)

_LOGGER = logging.getLogger(__name__)


@callback
def async_add_to_device(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """Add our config entry to the device."""
    device_registry = dr.async_get(hass)

    device_id = entry.data.get(CONF_DEVICE_ID)
    device_registry.async_update_device(device_id, add_config_entry_id=entry.entry_id)

    return device_id


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Initialize Battery Type config entry."""
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    async def async_registry_updated(event: Event) -> None:
        """Handle entity registry update."""
        data = event.data
        if data["action"] == "remove":
            await hass.config_entries.async_remove(config_entry.entry_id)

        if data["action"] != "update":
            return

        if "entity_id" in data["changes"]:
            # Entity_id replaced, reload the config entry
            await hass.config_entries.async_reload(config_entry.entry_id)

        if device_id and "device_id" in data["changes"]:
            # If the tracked battery note is no longer in the device, remove our config entry
            # from the device
            if (
                not (entity_entry := entity_registry.async_get(data[CONF_ENTITY_ID]))
                or not device_registry.async_get(device_id)
                or entity_entry.device_id == device_id
            ):
                # No need to do any cleanup
                return

            device_registry.async_update_device(
                device_id, remove_config_entry_id=config_entry.entry_id
            )

    config_entry.async_on_unload(
        async_track_entity_registry_updated_event(
            hass, config_entry.entry_id, async_registry_updated
        )
    )

    device_id = async_add_to_device(hass, config_entry)

    device = hass.data[DOMAIN][DATA].devices[config_entry.entry_id]

    coordinator = device.coordinator

    await coordinator.async_refresh()

    enable_replaced = True
    round_battery = False

    if DOMAIN_CONFIG in hass.data[DOMAIN]:
        domain_config: dict = hass.data[DOMAIN][DOMAIN_CONFIG]
        enable_replaced = domain_config.get(CONF_ENABLE_REPLACED, True)
        round_battery = domain_config.get(CONF_ROUND_BATTERY, False)

    battery_plus_sensor_entity_description = BatteryNotesSensorEntityDescription(
        unique_id_suffix="_battery_plus",
        key="battery_plus",
        translation_key="battery_plus",
        device_class=SensorDeviceClass.BATTERY,
        suggested_display_precision=0 if round_battery else 1,
    )

    type_sensor_entity_description = BatteryNotesSensorEntityDescription(
        unique_id_suffix="",  # battery_type has uniqueId set to entityId in V1, never add a suffix
        key="battery_type",
        translation_key="battery_type",
        icon="mdi:battery-unknown",
        entity_category=EntityCategory.DIAGNOSTIC,
    )

    last_replaced_sensor_entity_description = BatteryNotesSensorEntityDescription(
        unique_id_suffix="_battery_last_replaced",
        key="battery_last_replaced",
        translation_key="battery_last_replaced",
        icon="mdi:battery-clock",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_registry_enabled_default=enable_replaced,
    )

    entities = [
        BatteryNotesTypeSensor(
            hass,
            config_entry,
            coordinator,
            type_sensor_entity_description,
            f"{config_entry.entry_id}{type_sensor_entity_description.unique_id_suffix}",
        ),
        BatteryNotesLastReplacedSensor(
            hass,
            config_entry,
            coordinator,
            last_replaced_sensor_entity_description,
            f"{config_entry.entry_id}{last_replaced_sensor_entity_description.unique_id_suffix}",
        ),
    ]

    if device.wrapped_battery is not None:
        entities.append(
            BatteryNotesBatteryPlusSensor(
                hass,
                config_entry,
                coordinator,
                battery_plus_sensor_entity_description,
                f"{config_entry.entry_id}{battery_plus_sensor_entity_description.unique_id_suffix}",
                device,
                enable_replaced,
                round_battery,
            )
        )

    async_add_entities(entities)

    await coordinator.async_config_entry_first_refresh()


async def async_setup_platform(
    hass: HomeAssistant,
) -> None:
    """Set up the battery note sensor."""

    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)


class BatteryNotesBatteryPlusSensor(
    SensorEntity, CoordinatorEntity[BatteryNotesCoordinator]
):
    """Represents a battery plus type sensor."""

    _attr_should_poll = False
    _wrapped_attributes = None

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        coordinator: BatteryNotesCoordinator,
        description: BatteryNotesSensorEntityDescription,
        unique_id: str,
        device: BatteryNotesDevice,
        enable_replaced: bool,
        round_battery: bool,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        device_registry = dr.async_get(hass)

        self.config_entry = config_entry
        self.coordinator = coordinator
        self.entity_description = description
        self._attr_has_entity_name = True
        self._attr_unique_id = unique_id
        self.device = device
        self.enable_replaced = enable_replaced
        self.round_battery = round_battery

        self._device_id = coordinator.device_id
        if coordinator.device_id and (
            device_entry := device_registry.async_get(coordinator.device_id)
        ):
            self._attr_device_info = DeviceInfo(
                connections=device_entry.connections,
                identifiers=device_entry.identifiers,
            )

            self.entity_id = f"sensor.{device_entry.name.lower()}_{description.key}"

        entity_category = (
            device.wrapped_battery.entity_category if device.wrapped_battery else None
        )

        self._attr_entity_category = entity_category
        self._attr_unique_id = unique_id
        self._battery_entity_id = (
            device.wrapped_battery.entity_id if device.wrapped_battery else None
        )

        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = PERCENTAGE

    @callback
    def async_state_changed_listener(
        self, event: EventType[EventStateChangedData] | None = None
    ) -> None:
        # pylint: disable=unused-argument
        """Handle child updates."""

        if not self._battery_entity_id:
            return

        if (
            (wrapped_battery_state := self.hass.states.get(self._battery_entity_id))
            is None
            or wrapped_battery_state.state
            in [
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ]
            or not isfloat(wrapped_battery_state.state)
        ):
            self._attr_native_value = None
            self._attr_available = False
            self.async_write_ha_state()
            return

        self._attr_available = True

        if self.round_battery:
            self._attr_native_value = round(float(wrapped_battery_state.state), 0)
        else:
            self._attr_native_value = round(float(wrapped_battery_state.state), 1)

        self._wrapped_attributes = wrapped_battery_state.attributes

        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Handle added to Hass."""

        @callback
        def _async_state_changed_listener(
            event: EventType[EventStateChangedData] | None = None,
        ) -> None:
            """Handle child updates."""
            self.async_state_changed_listener(event)

        if self._battery_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._battery_entity_id], _async_state_changed_listener
                )
            )

        # Call once on adding
        _async_state_changed_listener()

        # Update entity options
        registry = er.async_get(self.hass)
        if registry.async_get(self.entity_id) is not None and self._battery_entity_id:
            registry.async_update_entity_options(
                self.entity_id,
                DOMAIN,
                {"entity_id": self._battery_entity_id},
            )

        if not (wrapped_battery := registry.async_get(self._battery_entity_id)):
            return

        if DOMAIN_CONFIG in self.hass.data[DOMAIN]:
            domain_config: dict = self.hass.data[DOMAIN][DOMAIN_CONFIG]
            hide_battery = domain_config.get(CONF_HIDE_BATTERY, False)
            if hide_battery:
                if wrapped_battery and not wrapped_battery.hidden:
                    registry.async_update_entity(
                        wrapped_battery.entity_id,
                        hidden_by=er.RegistryEntryHider.INTEGRATION,
                    )
            else:
                if (
                    wrapped_battery
                    and wrapped_battery.hidden_by == er.RegistryEntryHider.INTEGRATION
                ):
                    registry.async_update_entity(
                        wrapped_battery.entity_id, hidden_by=None
                    )

        def copy_custom_name(wrapped_battery: er.RegistryEntry) -> None:
            """Copy the name set by user from the wrapped entity."""
            if wrapped_battery.name is None:
                return
            registry.async_update_entity(
                self.entity_id, name=wrapped_battery.name + "+"
            )

        copy_custom_name(wrapped_battery)

        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

        await self.coordinator.async_config_entry_first_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        _LOGGER.debug("Update from coordinator")

        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return the state attributes of the battery type."""

        attrs = {
            ATTR_BATTERY_QUANTITY: self.coordinator.battery_quantity,
            ATTR_BATTERY_TYPE: self.coordinator.battery_type,
            ATTR_BATTERY_TYPE_AND_QUANTITY: self.coordinator.battery_type_and_quantity,
            ATTR_BATTERY_LOW: self.coordinator.battery_low,
            ATTR_BATTERY_LOW_THRESHOLD: self.coordinator.battery_low_threshold,
        }

        if self.enable_replaced:
            attrs[ATTR_BATTERY_LAST_REPLACED] = self.coordinator.last_replaced

        super_attrs = super().extra_state_attributes
        if super_attrs:
            attrs.update(super_attrs)
        if self._wrapped_attributes:
            attrs.update(self._wrapped_attributes)
        return attrs

    @property
    def native_value(self) -> StateType:
        """Return the value reported by the sensor."""
        return self._attr_native_value


class BatteryNotesTypeSensor(RestoreSensor, SensorEntity):
    """Represents a battery note type sensor."""

    _attr_should_poll = False
    entity_description: BatteryNotesSensorEntityDescription

    def __init__(
        self,
        hass,
        config_entry: ConfigEntry,
        coordinator: BatteryNotesCoordinator,
        description: BatteryNotesSensorEntityDescription,
        unique_id: str,
    ) -> None:
        # pylint: disable=unused-argument
        """Initialize the sensor."""
        super().__init__()

        device_registry = dr.async_get(hass)

        self.coordinator = coordinator
        self.entity_description = description
        self._attr_has_entity_name = True
        self._attr_unique_id = unique_id
        self._device_id = coordinator.device_id

        if coordinator.device_id and (
            device_entry := device_registry.async_get(coordinator.device_id)
        ):
            self._attr_device_info = DeviceInfo(
                connections=device_entry.connections,
                identifiers=device_entry.identifiers,
            )

            self.entity_id = f"sensor.{device_entry.name.lower()}_{description.key}"

        self._battery_type = coordinator.battery_type
        self._battery_quantity = coordinator.battery_quantity

    async def async_added_to_hass(self) -> None:
        """Handle added to Hass."""
        await super().async_added_to_hass()
        state = await self.async_get_last_sensor_data()
        if state:
            self._attr_native_value = state.native_value

        # Update entity options
        registry = er.async_get(self.hass)
        if registry.async_get(self.entity_id) is not None:
            registry.async_update_entity_options(
                self.entity_id,
                DOMAIN,
                {
                    "entity_id": self._attr_unique_id,
                },
            )

    @property
    def native_value(self) -> str:
        """Return the native value of the sensor."""
        return self.coordinator.battery_type_and_quantity

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return the state attributes of the battery type."""

        attrs = {
            ATTR_BATTERY_QUANTITY: self.coordinator.battery_quantity,
            ATTR_BATTERY_TYPE: self.coordinator.battery_type,
        }

        super_attrs = super().extra_state_attributes
        if super_attrs:
            attrs.update(super_attrs)
        return attrs


class BatteryNotesLastReplacedSensor(
    SensorEntity, CoordinatorEntity[BatteryNotesCoordinator]
):
    """Represents a battery note sensor."""

    _attr_should_poll = False
    entity_description: BatteryNotesSensorEntityDescription
    _battery_entity_id = None

    def __init__(
        self,
        hass,
        config_entry: ConfigEntry,
        coordinator: BatteryNotesCoordinator,
        description: BatteryNotesSensorEntityDescription,
        unique_id: str,
    ) -> None:
        # pylint: disable=unused-argument
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_device_class = description.device_class
        self._attr_has_entity_name = True
        self._attr_unique_id = unique_id
        self._device_id = coordinator.device_id
        self.entity_description = description
        self._native_value = None

        self._set_native_value(log_on_error=False)

        device_registry = dr.async_get(hass)

        if coordinator.device_id and (
            device_entry := device_registry.async_get(coordinator.device_id)
        ):
            self._attr_device_info = DeviceInfo(
                connections=device_entry.connections,
                identifiers=device_entry.identifiers,
            )

            self.entity_id = f"sensor.{device_entry.name.lower()}_{description.key}"

    async def async_added_to_hass(self) -> None:
        """Handle added to Hass."""
        await super().async_added_to_hass()

    def _set_native_value(self, log_on_error=True):
        # pylint: disable=unused-argument
        device_entry = self.coordinator.store.async_get_device(self._device_id)
        if device_entry:
            if LAST_REPLACED in device_entry:
                last_replaced_date = datetime.fromisoformat(
                    str(device_entry[LAST_REPLACED]) + "+00:00"
                )
                self._native_value = last_replaced_date

                return True
        return False

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        device_entry = self.coordinator.store.async_get_device(self._device_id)
        if device_entry:
            if LAST_REPLACED in device_entry:
                last_replaced_date = datetime.fromisoformat(
                    str(device_entry[LAST_REPLACED]) + "+00:00"
                )
                self._native_value = last_replaced_date

                self.async_write_ha_state()

    @property
    def native_value(self) -> datetime | None:
        """Return the native value of the sensor."""
        return self._native_value
