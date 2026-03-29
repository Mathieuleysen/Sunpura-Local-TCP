"""DataUpdateCoordinator for the AECC Local Battery integration."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any, Dict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN, POLL_INTERVAL, MIN_POLL_INTERVAL, MAX_BATTERY_POWER_W,
    MODE_REGISTERS, REG_MIN_SOC, REG_MAX_SOC,
)
from .tcp_client import AECCBatteryClient

_LOGGER = logging.getLogger(__name__)

# ── Register map (confirmed from live scan) ───────────────────────────────────
# 3000  EMS enable           1 = on
# 3003  controlTime1         active power schedule slot  ← we write here
# 3023  min discharge SOC    (10 %)
# 3024  max charge SOC       (98 %)
# 3030  custom mode          1 = on
#
# Time-slot format (11 fields):
#   "switch,start,end,power,temp,mode,0,0,0,chargingSOC,dischargingSOC"
#   e.g. "1,14:02,23:59,-2400,0,6,0,0,0,100,10"
#
# SIGN CONVENTION (confirmed empirically and from live cloud behaviour):
#   register negative → charge    (e.g. -2400 = charge at 2400 W)
#   register positive → discharge (e.g. +2400 = discharge at 2400 W)
#
# FAST-PATH DISCOVERY (from monitor data, 2025-03):
#   The cloud writes ONLY register 3003, using the current wall-clock minute
#   as the start time.  It never touches 3004–3018, 3020, 3025, 3026, or 3030.
#   Writing those extra registers is what caused the 12–15 s processing delay.
#   Mimicking the cloud (2-register write: 3000 + 3003) drops latency to ~2 s.
# ─────────────────────────────────────────────────────────────────────────────

_SLOT_DISABLED   = "0,00:00,00:00,0,0,0,0,0,0,100,10"
_CHARGING_SOC    = 100
_DISCHARGING_SOC = 10


def _now_hhmm() -> str:
    """Return the current local time as HH:MM — used as the slot start time.

    The cloud always writes the current minute as the start time so the
    schedule is immediately active.  Using 00:00 also works but the cloud
    doesn't do it, and it may interact differently with the firmware.
    """
    return datetime.now().strftime("%H:%M")


class AECCLocalCoordinator(DataUpdateCoordinator[Dict[str, Any]]):

    def __init__(self, hass: HomeAssistant, client: AECCBatteryClient,
                 device_name: str, poll_interval: int = POLL_INTERVAL) -> None:
        self.client = client
        self.device_name = device_name
        self._last_set_response: Any = "never sent"
        self._register_scan: Any = None
        self._consecutive_failures: int = 0
        self._last_good_data: Dict[str, Any] | None = None
        # How many consecutive poll failures to tolerate before marking unavailable.
        # Battery stops responding for a few seconds after a SET command; at 2 s
        # polling that is ~2–3 failures with the fast path.  We allow 5 (= 10 s)
        # as a safety margin so entities stay showing their last known values
        # rather than flickering to unknown.
        self._failure_tolerance: int = 5
        super().__init__(
            hass, _LOGGER,
            name=f"{DOMAIN}_{device_name}",
            update_interval=timedelta(seconds=max(poll_interval, MIN_POLL_INTERVAL)),
        )

    async def _async_setup(self) -> None:
        await self.client.async_connect()

    async def _async_update_data(self) -> Dict[str, Any]:
        raw = await self.client.get_energy_parameters()

        # Treat as a failure if:
        #  • No response (None)
        #  • Response lacks Storage_list / SSumInfoList  ← battery returns
        #    an incomplete response while processing a SET command, which
        #    would make all sensor values None and show "unknown" in the UI.
        valid = (
            raw is not None
            and raw.get("Storage_list")
            and raw.get("SSumInfoList")
        )

        if not valid:
            self._consecutive_failures += 1
            if (self._consecutive_failures <= self._failure_tolerance
                    and self._last_good_data is not None):
                _LOGGER.debug(
                    "Incomplete/missing poll response (%d/%d) — keeping last known data",
                    self._consecutive_failures, self._failure_tolerance,
                )
                return self._last_good_data
            raise UpdateFailed(
                f"No valid response from {self.client.host}:{self.client.port} "
                f"after {self._consecutive_failures} consecutive failures"
            )

        self._consecutive_failures = 0
        self._last_good_data = raw
        return raw

    # ── Accessors ─────────────────────────────────────────────────────────────

    @property
    def storage(self) -> Dict[str, Any]:
        if not self.data:
            return {}
        return (self.data.get("Storage_list") or [{}])[0]

    @property
    def summary(self) -> Dict[str, Any]:
        return self.data.get("SSumInfoList", {}) if self.data else {}

    _STORAGE_POWER_KEYS = {
        "PvChargingPower", "AcChargingPower", "BatteryDischargingPower",
        "AcInActivePower", "OffGridLoadPower", "BatteryChargingPower",
        "Pv1Power", "Pv2Power", "Pv3Power", "Pv4Power",
    }

    def storage_val(self, key: str, default: Any = None) -> Any:
        val = self.storage.get(key, default)
        if val is None:
            return default
        if key in self._STORAGE_POWER_KEYS:
            try:
                return round(float(val) / 10, 1)
            except (TypeError, ValueError):
                return val
        return val

    def summary_val(self, key: str, default: Any = None) -> Any:
        return self.summary.get(key, default)

    # ── Power setpoint ─────────────────────────────────────────────────────────

    async def async_set_power_setpoint(self, watts: float) -> bool:
        """Write the power setpoint to register 3003 (controlTime1).

        FAST PATH — mirrors the cloud app's behaviour exactly:

          • Write ONLY register 3000 (EMS enable) and 3003 (time slot).
          • Use the current wall-clock minute as the slot start time so the
            schedule is immediately active (same as the cloud).
          • Never touch 3004–3018, 3020, 3025, 3026, or 3030 — writing those
            extra registers causes a 12–15 s firmware processing pause.

        Sign convention (confirmed empirically):
            UI +2400 W = charge    → register value -2400
            UI -2400 W = discharge → register value +2400
        """
        power_w = int(watts)
        reg_power = -power_w   # invert sign for battery's convention

        start_time = _now_hhmm()

        if power_w == 0:
            # Disabled slot — stops any active charge/discharge command.
            slot1 = _SLOT_DISABLED
        else:
            # Active slot: start=now, end=23:59, power=reg_power (inverted sign).
            slot1 = (
                f"1,{start_time},23:59,{reg_power},0,6,0,0,0,"
                f"{_CHARGING_SOC},{_DISCHARGING_SOC}"
            )

        # Minimal 2-register payload — exactly what the cloud sends.
        payload = {
            "3000": "1",    # EMS enable
            "3003": slot1,  # controlTime1 — the only setpoint register needed
        }

        _LOGGER.info(
            "AECC SET power %+d W → reg_power=%+d → 3003=%r",
            power_w, reg_power, slot1,
        )

        resp = await self.client.set_control_parameters(payload)
        self._last_set_response = resp
        return resp is not None

    # ── Work mode & SOC ────────────────────────────────────────────────────────

    async def async_set_work_mode(self, mode: str) -> bool:
        registers = MODE_REGISTERS.get(mode)
        if registers is None:
            return False
        resp = await self.client.set_control_parameters(registers)
        self._last_set_response = resp
        return resp is not None

    async def async_set_min_soc(self, value: int) -> bool:
        resp = await self.client.set_control_parameters({"3023": str(value)})
        self._last_set_response = resp
        return resp is not None

    async def async_set_max_soc(self, value: int) -> bool:
        resp = await self.client.set_control_parameters({"3002": str(value)})
        self._last_set_response = resp
        return resp is not None

    # ── Register scan ──────────────────────────────────────────────────────────

    async def async_scan_power_registers(self) -> None:
        all_addrs = list(range(3000, 3150)) + list(range(4000, 4050))
        batch_size = 10
        all_results: dict = {}
        raw_batches: list = []

        for i in range(0, len(all_addrs), batch_size):
            batch = all_addrs[i:i + batch_size]
            resp = await self.client.get_control_parameters(batch)
            if resp is None:
                continue
            raw_batches.append(resp)
            params = (resp.get("ControlInfo")
                      or resp.get("GetParameters")
                      or resp.get("Parameters")
                      or resp.get("SetParameters")
                      or {})
            if isinstance(params, dict):
                for addr, val in params.items():
                    str_val = str(val).strip()
                    if str_val not in ("", "0", "None"):
                        all_results[addr] = str_val

        self._register_scan = {
            "non_empty_registers": all_results,
            "all_batches": raw_batches,
        }
        _LOGGER.info("AECC register scan: %s", all_results)
