"""Sensor platform – AC Charging Power, Battery Discharging Power, Battery SOC."""
from __future__ import annotations
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import CONF_HOST, CONF_NAME, CONF_PORT, DOMAIN
from .coordinator import AECCLocalCoordinator

_SENSORS = [
    ("ac_charging_power",       "AC Charging Power",        "AcChargingPower",        UnitOfPower.WATT, "mdi:power-plug",          True),
    ("battery_discharging_power","Battery Discharging Power","BatteryDischargingPower", UnitOfPower.WATT, "mdi:battery-arrow-down",  True),
    ("battery_soc",             "Battery SOC",              "BatterySoc",             PERCENTAGE,       "mdi:battery",             False),
]

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: AECCLocalCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([
        AECCSensor(coordinator, config_entry, key, name, storage_key, unit, icon, is_power)
        for key, name, storage_key, unit, icon, is_power in _SENSORS
    ])

class AECCSensor(CoordinatorEntity[AECCLocalCoordinator], SensorEntity):
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
        self._last_value = None  # cache last non-None value

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._config_entry.data[CONF_HOST]}:{self._config_entry.data[CONF_PORT]}")},
            name=self._config_entry.data[CONF_NAME],
            manufacturer="AECC", model="EMS Battery Hub",
        )

    @property
    def native_value(self):
        val = self.coordinator.storage_val(self._storage_key)
        if val is not None:
            self._last_value = val
            return val
        # Return last known value so sensor stays populated during battery processing
        return self._last_value

    @property
    def available(self) -> bool:
        return self._last_value is not None or self.coordinator.last_update_success
