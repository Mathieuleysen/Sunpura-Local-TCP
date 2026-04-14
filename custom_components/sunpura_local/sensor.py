"""Sensor platform – AC Charging Power, Battery Discharging Power, Battery SOC,
Energy Charged, Energy Discharged."""
from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.dt import utcnow

from .const import CONF_HOST, CONF_NAME, CONF_PORT, DOMAIN
from .coordinator import SunpuraLocalCoordinator

_LOGGER = logging.getLogger(__name__)

# ── Standard power / measurement sensors ──────────────────────────────────────
# (key, name, storage_key, unit, icon, is_power)
_SENSORS = [
    ("ac_charging_power",         "AC Charging Power",         "AcChargingPower",         UnitOfPower.WATT, "mdi:power-plug",         True),
    ("battery_discharging_power", "Battery Discharging Power", "BatteryDischargingPower",  UnitOfPower.WATT, "mdi:battery-arrow-down", True),
    ("battery_soc",               "Battery SOC",               "BatterySoc",              PERCENTAGE,       "mdi:battery",            False),
]

# ── Energy counter definitions ─────────────────────────────────────────────────
# (key, name, list_of_storage_keys, icon)
# Storage keys must match those used by coordinator.storage_val()
_ENERGY_SENSORS = [
    (
        "energy_charged",
        "Energy Charged",
        ["AcChargingPower"],          # AC grid → battery charging power
        "mdi:battery-charging",
    ),
    (
        "energy_discharged",
        "Energy Discharged",
        ["BatteryDischargingPower"],  # battery → load discharging power
        "mdi:battery-arrow-down-outline",
    ),
]

# Skip accumulation if time gap exceeds this (avoids phantom spikes after HA restarts)
_MAX_GAP_SECONDS = 60


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SunpuraLocalCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities: list[SensorEntity] = []

    # Standard measurement sensors
    for key, name, storage_key, unit, icon, is_power in _SENSORS:
        entities.append(
            SunpuraSensor(coordinator, config_entry, key, name, storage_key, unit, icon, is_power)
        )

    # Energy counter sensors (Riemann sum, state restored across restarts)
    for key, name, storage_keys, icon in _ENERGY_SENSORS:
        entities.append(
            SunpuraEnergySensor(coordinator, config_entry, key, name, storage_keys, icon)
        )

    async_add_entities(entities)


# ── Standard measurement sensor ───────────────────────────────────────────────

class SunpuraSensor(CoordinatorEntity[SunpuraLocalCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, config_entry, key, name, storage_key, unit, icon, is_power):
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._storage_key = storage_key
        self._is_power = is_power
        self._attr_unique_id = f"{config_entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_device_class = SensorDeviceClass.POWER if is_power else SensorDeviceClass.BATTERY
        self._last_value = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._config_entry.data[CONF_HOST]}:{self._config_entry.data[CONF_PORT]}")},
            name=self._config_entry.data[CONF_NAME],
            manufacturer="Mathieu",
            model="EMS Battery Hub for Sunpura",
        )

    @property
    def native_value(self):
        val = self.coordinator.storage_val(self._storage_key)
        if val is not None:
            self._last_value = val
            return val
        return self._last_value

    @property
    def available(self) -> bool:
        return self._last_value is not None or self.coordinator.last_update_success


# ── Energy counter (Riemann sum + RestoreEntity) ──────────────────────────────

class SunpuraEnergySensor(
    CoordinatorEntity[SunpuraLocalCoordinator], RestoreEntity, SensorEntity
):
    """Accumulated energy (kWh) computed by integrating power over time.

    Uses RestoreEntity so the running total survives Home Assistant restarts.
    Power values are read via coordinator.storage_val() which already converts
    the raw tenths-of-a-watt values to watts (÷10).
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator, config_entry, key, name, storage_keys, icon):
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._storage_keys = storage_keys
        self._attr_unique_id = f"{config_entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._accumulated_kwh: float = 0.0
        self._last_update_time: datetime | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._config_entry.data[CONF_HOST]}:{self._config_entry.data[CONF_PORT]}")},
            name=self._config_entry.data[CONF_NAME],
            manufacturer="Mathieu",
            model="EMS Battery Hub for Sunpura",
        )

    @property
    def native_value(self) -> float:
        return round(self._accumulated_kwh, 3)

    async def async_added_to_hass(self) -> None:
        """Restore accumulated kWh from the last known state after a restart."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in ("unknown", "unavailable"):
            try:
                self._accumulated_kwh = float(last_state.state)
            except (TypeError, ValueError):
                self._accumulated_kwh = 0.0
        # Leave _last_update_time = None so the first poll sets a clean baseline
        # without adding a spurious energy spike.

    @callback
    def _handle_coordinator_update(self) -> None:
        """Called on every coordinator poll – integrate power → energy."""
        now = utcnow()

        # Sum all relevant power sources (in watts, already converted by storage_val)
        total_power_w = 0.0
        any_valid = False
        for key in self._storage_keys:
            val = self.coordinator.storage_val(key)
            if val is not None:
                try:
                    total_power_w += float(val)
                    any_valid = True
                except (TypeError, ValueError):
                    pass

        if any_valid and self._last_update_time is not None:
            delta_seconds = (now - self._last_update_time).total_seconds()
            if 0 < delta_seconds <= _MAX_GAP_SECONDS:
                delta_kwh = total_power_w * delta_seconds / 3_600_000
                self._accumulated_kwh += delta_kwh

        if any_valid:
            self._last_update_time = now

        self.async_write_ha_state()
