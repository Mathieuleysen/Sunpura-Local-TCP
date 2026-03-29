"""Switch platform – EMS master enable/disable for AECC Local Battery."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_HOST, CONF_NAME, CONF_PORT, DOMAIN, REG_EMS_ENABLE
from .coordinator import AECCLocalCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AECCLocalCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([AECCEmsSwitch(coordinator, config_entry)])


class AECCEmsSwitch(CoordinatorEntity[AECCLocalCoordinator], SwitchEntity):
    """Master EMS on/off switch — mirrors ControlEnableStatus (register 3000)."""

    _attr_icon = "mdi:battery-sync"
    _attr_has_entity_name = True
    _attr_name = "EMS Enabled"

    def __init__(
        self,
        coordinator: AECCLocalCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_ems_enabled"
        self._optimistic: bool | None = None

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
    def is_on(self) -> bool | None:
        # Live value from device takes priority once a poll has succeeded
        val = self.coordinator.summary.get("ControlEnableStatus")
        if val is not None:
            return bool(int(val))
        return self._optimistic

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    async def async_turn_on(self, **kwargs) -> None:
        resp = await self.coordinator.client.set_control_parameters(
            {REG_EMS_ENABLE: "1"}
        )
        if resp is not None:
            self._optimistic = True
            self.async_write_ha_state()
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error("Failed to enable EMS")

    async def async_turn_off(self, **kwargs) -> None:
        resp = await self.coordinator.client.set_control_parameters(
            {REG_EMS_ENABLE: "0"}
        )
        if resp is not None:
            self._optimistic = False
            self.async_write_ha_state()
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error("Failed to disable EMS")
