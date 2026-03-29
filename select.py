"""Select platform – battery work mode for AECC Local Battery."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_HOST, CONF_NAME, CONF_PORT, DOMAIN, WORK_MODES
from .coordinator import AECCLocalCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AECCLocalCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([AECCWorkModeSelect(coordinator, config_entry)])


class AECCWorkModeSelect(CoordinatorEntity[AECCLocalCoordinator], SelectEntity):
    """Dropdown to switch the battery between Self-Consumption, Custom, and Disabled."""

    _attr_icon = "mdi:battery-sync"
    _attr_has_entity_name = True
    _attr_name = "Work Mode"
    _attr_options = WORK_MODES

    def __init__(
        self,
        coordinator: AECCLocalCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_work_mode"
        self._current_mode: str | None = None

    @property
    def device_info(self) -> DeviceInfo:
        host = self._config_entry.data[CONF_HOST]
        port = self._config_entry.data[CONF_PORT]
        name = self._config_entry.data[CONF_NAME]
        return DeviceInfo(
            identifiers={(DOMAIN, f"{host}:{port}")},
            name=name,
            manufacturer="AECC",
            model="EMS Battery Hub",
        )

    @property
    def current_option(self) -> str | None:
        """Return last-known mode (persisted across polls)."""
        return self._current_mode

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    async def async_select_option(self, option: str) -> None:
        """User picked a new mode from the UI dropdown."""
        _LOGGER.info("User selected work mode: %s", option)
        success = await self.coordinator.async_set_work_mode(option)
        if success:
            self._current_mode = option
            self.async_write_ha_state()
        else:
            _LOGGER.error("Failed to set work mode to '%s'", option)
