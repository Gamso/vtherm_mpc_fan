"""Data collection module for VTherm MPC Fan.

Writes one CSV row per control cycle so that algorithm behaviour can be
reproduced offline and used to identify improvement areas during the beta.

CSV columns (in order):
  timestamp, hvac_mode, current_temp, target_temp, current_error,
  vtherm_slope, effective_slope, projected_temp, projected_error,
  phase, minutes_since_change, effective_timeout, current_fan, decided_fan,
  force, reason, learning_ready, dead_time, is_window_open,
  mpc_status, mpc_fan,
  mpc_would_change, mpc_cost, mpc_confidence,
  mpc_temp_10m, mpc_temp_30m, mpc_known_profiles,
  mpc_disturbance, defrost_active, hvac_idle, outdoor_temp,
  user_target_temp, regulation_offset, comfort_error

``target_temp`` and ``current_error`` are the *regulated* setpoint VTherm sends
to the underlying (``regulated_target_temperature``) and the error against it,
as they always were. ``user_target_temp`` is the user's own setpoint
(``target_temperature``), ``regulation_offset`` is regulated minus user (degC,
raw, not sign-aligned), and ``comfort_error`` is the signed error against the
user's setpoint (positive = needs more heating/cooling). New columns are only
ever appended, so scripts that index the historical ones keep working.
"""

import asyncio
import csv
import logging
import os
from datetime import datetime, timezone

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Header used when creating a new file. Keep in sync with async_record() below.
_HEADER = [
    "timestamp",
    "hvac_mode",
    "current_temp",
    "target_temp",
    "current_error",
    "vtherm_slope",
    "effective_slope",
    "projected_temp",
    "projected_error",
    "phase",
    "minutes_since_change",
    "effective_timeout",
    "current_fan",
    "decided_fan",
    "force",
    "reason",
    "learning_ready",
    "dead_time",
    "is_window_open",
    "mpc_status",
    "mpc_fan",
    "mpc_would_change",
    "mpc_cost",
    "mpc_confidence",
    "mpc_temp_10m",
    "mpc_temp_30m",
    "mpc_known_profiles",
    "mpc_disturbance",
    "defrost_active",
    "hvac_idle",
    "outdoor_temp",
    "user_target_temp",
    "regulation_offset",
    "comfort_error",
]

# Rotate the file when it exceeds this size (bytes). 10 MB keeps ~200 000 rows.
_MAX_FILE_SIZE = 10 * 1024 * 1024


def _coalesce(value, default):
    """Return *value* if it is not None, otherwise *default*."""
    return value if value is not None else default


class DataCollector:
    """Appends one CSV row per control cycle to a rotating log file."""

    def __init__(self, hass: HomeAssistant, config_dir: str, entry_id: str) -> None:
        self._hass = hass
        self._path = os.path.join(config_dir, f"vtherm_mpc_fan_data_{entry_id[:8]}.csv")
        base, ext = os.path.splitext(self._path)
        self._rotated_path = f"{base}_old{ext}"
        self._io_lock = asyncio.Lock()

    async def async_initialize(self) -> None:
        """Create or validate the CSV file outside the event loop."""
        async with self._io_lock:
            await self._hass.async_add_executor_job(self._ensure_header)

    async def async_record(
        self,
        *,
        hvac_mode: str,
        current_temp: float,
        target_temp: float,
        vtherm_slope: float,
        is_window_open: bool,
        decision: dict,
        phase: str,
        effective_slope: float,
        effective_timeout: float,
        force: bool,
        learning_ready: bool,
        dead_time: float,
        mpc_decision: dict | None = None,
        defrost_active: bool = False,
        is_hvac_idle: bool = False,
        outdoor_temp: float | None = None,
        user_target_temp: float | None = None,
        regulation_offset: float | None = None,
        comfort_error: float | None = None,
    ) -> None:
        """Append one row to the CSV file outside the event loop."""
        mpc = mpc_decision or {}
        row = [
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            hvac_mode,
            round(current_temp, 3),
            round(target_temp, 3),
            round(decision.get("temperature_error", 0.0), 3),
            round(vtherm_slope, 4),
            round(effective_slope, 4),
            round(_coalesce(decision.get("projected_temperature"), current_temp), 3),
            round(_coalesce(decision.get("projected_temperature_error"), 0.0), 3),
            phase,
            round(decision.get("minutes_since_last_change", 0.0), 2),
            round(effective_timeout, 2),
            decision.get("current_fan", ""),
            decision.get("fan_mode", ""),
            int(force),
            decision.get("reason", ""),
            int(learning_ready),
            round(dead_time, 2),
            int(is_window_open),
            mpc.get("mpc_status", "Not ready"),
            mpc.get("mpc_fan_mode", ""),
            mpc.get("mpc_would_change_now", "no"),
            round(mpc.get("mpc_cost", 0.0), 3) if mpc.get("mpc_cost") is not None else "",
            round(mpc.get("mpc_confidence", 0.0), 1) if mpc.get("mpc_confidence") is not None else "",
            round(mpc.get("mpc_predicted_temperature_10m", 0.0), 3) if mpc.get("mpc_predicted_temperature_10m") is not None else "",
            round(mpc.get("mpc_predicted_temperature_30m", 0.0), 3) if mpc.get("mpc_predicted_temperature_30m") is not None else "",
            mpc.get("mpc_known_profiles", 0),
            round(mpc.get("mpc_disturbance_bias", 0.0), 3) if mpc.get("mpc_disturbance_bias") is not None else "",
            int(defrost_active),
            int(is_hvac_idle),
            round(outdoor_temp, 2) if outdoor_temp is not None else "",
            round(user_target_temp, 3) if user_target_temp is not None else "",
            round(regulation_offset, 3) if regulation_offset is not None else "",
            round(comfort_error, 3) if comfort_error is not None else "",
        ]
        async with self._io_lock:
            await self._hass.async_add_executor_job(self._write_row, row)

    @property
    def path(self) -> str:
        """Return the active CSV file path."""
        return self._path

    def _write_row(self, row: list[object]) -> None:
        """Write one row to disk, rotating the file first if needed."""
        try:
            self._rotate_if_needed()
            self._ensure_header()
            with open(self._path, "a", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(row)
        except OSError as exc:
            _LOGGER.warning("DataCollector: could not write to %s: %s", self._path, exc)

    def _ensure_header(self) -> None:
        """Write the header row if missing, or rotate when the schema changes."""
        if not os.path.exists(self._path):
            try:
                with open(self._path, "w", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerow(_HEADER)
                _LOGGER.info("DataCollector: created %s", self._path)
            except OSError as exc:
                _LOGGER.warning("DataCollector: could not create %s: %s", self._path, exc)
            return

        try:
            with open(self._path, newline="", encoding="utf-8") as fh:
                current_header = next(csv.reader(fh), [])
            if current_header != _HEADER:
                if os.path.exists(self._rotated_path):
                    os.remove(self._rotated_path)
                os.rename(self._path, self._rotated_path)
                with open(self._path, "w", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerow(_HEADER)
                _LOGGER.info("DataCollector: rotated %s due to header change", self._path)
        except OSError as exc:
            _LOGGER.warning("DataCollector: could not validate %s: %s", self._path, exc)

    def _rotate_if_needed(self) -> None:
        """Rename current file to *_old.csv when it exceeds _MAX_FILE_SIZE."""
        try:
            if os.path.exists(self._path) and os.path.getsize(self._path) >= _MAX_FILE_SIZE:
                if os.path.exists(self._rotated_path):
                    os.remove(self._rotated_path)
                os.rename(self._path, self._rotated_path)
                self._ensure_header()
                _LOGGER.info("DataCollector: rotated %s -> %s", self._path, self._rotated_path)
        except OSError as exc:
            _LOGGER.warning("DataCollector: rotation error for %s: %s", self._path, exc)
