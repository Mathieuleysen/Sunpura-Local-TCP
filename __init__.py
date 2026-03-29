"""AECC Battery – local TCP integration for Home Assistant."""

from __future__ import annotations

import json
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_HOST, CONF_NAME, CONF_PORT, DEFAULT_TIMEOUT, DOMAIN
from .coordinator import AECCLocalCoordinator
from .tcp_client import AECCBatteryClient
from .tcp_manager import TCPClientManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.NUMBER]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    host: str = entry.data[CONF_HOST]
    port: int = entry.data[CONF_PORT]
    name: str = entry.data[CONF_NAME]

    client = AECCBatteryClient(host, port, timeout=DEFAULT_TIMEOUT)
    try:
        await client.async_connect()
    except Exception as exc:
        raise ConfigEntryNotReady(f"Cannot connect to {host}:{port} – {exc}") from exc

    coordinator = AECCLocalCoordinator(hass, client, name)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # ── Services ──────────────────────────────────────────────────────────────

    async def handle_try_command(call: ServiceCall) -> None:
        """Send any Get or Set command to the battery and log the response.
        Use this from Developer Tools → Services to probe the battery's
        command set and find working power control commands.
        """
        direction = call.data.get("direction", "Get").strip()
        command   = call.data.get("command",   "").strip()
        raw_payload = call.data.get("payload",  "")

        if not command:
            _LOGGER.error("try_command: 'command' field is required")
            return

        payload: dict = {}
        if raw_payload:
            if isinstance(raw_payload, dict):
                payload = raw_payload
            else:
                try:
                    payload = json.loads(str(raw_payload))
                except json.JSONDecodeError:
                    _LOGGER.error("try_command: 'payload' is not valid JSON: %s", raw_payload)
                    return

        _LOGGER.warning("AECC try_command → %s %s  payload=%s", direction, command, payload)

        if direction.lower() == "set":
            resp = await coordinator.client.send_set(command, payload or None)
        else:
            resp = await coordinator.client.send_get(command, payload or None)

        coordinator._last_set_response = resp
        _LOGGER.warning("AECC try_command ← %s", resp)
        await coordinator.async_request_refresh()

    async def handle_read_registers(call: ServiceCall) -> None:
        addresses = call.data.get("addresses", [])
        if not addresses:
            return
        _LOGGER.warning("AECC read_registers → %s", addresses)
        resp = await coordinator.client.get_control_parameters(addresses)
        # Surface ControlInfo (the actual key the battery uses) prominently
        info = (resp or {}).get("ControlInfo") or {}
        coordinator._register_scan = {
            "ControlInfo": info,
            "non_empty": {k: v for k, v in info.items() if str(v).strip() not in ("", "0", "None")},
            "raw": resp,
        }
        _LOGGER.warning("AECC read_registers ← ControlInfo=%s", info)
        await coordinator.async_request_refresh()

    async def handle_set_raw_register(call: ServiceCall) -> None:
        address = str(call.data.get("address", "")).strip()
        value   = str(call.data.get("value",   "")).strip()
        if not address or not value:
            return
        _LOGGER.warning("AECC set_raw_register → [%s]=%r", address, value)
        resp = await coordinator.client.set_control_parameters({address: value})
        coordinator._last_set_response = resp
        _LOGGER.warning("AECC set_raw_register ← %s", resp)
        await coordinator.async_request_refresh()

    async def handle_scan_power_registers(call) -> None:
        """Scan registers 3000-3150 and 4000-4050 for non-zero values."""
        _LOGGER.warning("AECC scanning registers for non-zero power values ...")
        await coordinator.async_scan_power_registers()
        _LOGGER.warning("AECC scan done — check register_scan sensor attributes.")

    hass.services.async_register(DOMAIN, "scan_power_registers", handle_scan_power_registers)

    async def handle_probe_fast_command(call) -> None:
        """Try commands that might bypass the 15-second firmware loop.
        Use this to find the fast-path command the cloud server uses.
        Checks: DirectPower, ForcedPower, PowerControl, EnergyControl, SubDeviceControl.
        Results appear in last_set_response on the Power Setpoint entity.
        """
        power = int(call.data.get("power", 1000))
        from datetime import datetime
        start = datetime.now().strftime("%H:%M")

        candidates = [
            # (command, payload)
            ("DirectPower",   {"Power": power}),
            ("ForcedPower",   {"Power": power, "DevAddr": 1}),
            ("PowerControl",  {"Power": power, "DevAddr": 1}),
            ("EnergyControl", {"ForcedPower": power, "DevAddr": 1}),
            # SubDeviceControl with battery DevAddr=1
            ("SubDeviceControl", {"ControlsParameter": {
                "DevTypeClass": 768, "DevAddr": 1, "IsThirdParty": False,
                "CommSerialNum": 1, "DevType": 131, "Param": {"Power": power}
            }}),
        ]
        results = {}
        for cmd, payload in candidates:
            resp = await coordinator.client.send_set(cmd, payload)
            results[cmd] = resp
            _LOGGER.warning("AECC probe_fast_command [%s] → %s", cmd, resp)
            # Brief pause between attempts
            import asyncio
            await asyncio.sleep(0.5)

        coordinator._last_set_response = {"probe_results": results}
        _LOGGER.warning("AECC probe_fast_command complete: %s", results)

    hass.services.async_register(DOMAIN, "probe_fast_command", handle_probe_fast_command)
    hass.services.async_register(DOMAIN, "try_command",        handle_try_command)
    hass.services.async_register(DOMAIN, "read_registers",     handle_read_registers)
    hass.services.async_register(DOMAIN, "set_raw_register",   handle_set_raw_register)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _LOGGER.info("AECC Local Battery '%s' set up at %s:%s", name, host, port)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: AECCLocalCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.client.async_disconnect()
        TCPClientManager.remove_instance(entry.data[CONF_HOST], entry.data[CONF_PORT])
    return unloaded
