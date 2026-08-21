"""Tests for async-safe CSV data collection."""

import csv
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.vtherm_mpc_fan.manager import (
    MpcFanFeatureManager,
    build_data_collection_decision as _build_data_collection_decision,
)
from custom_components.vtherm_mpc_fan.const import CONF_DATA_COLLECTION
from custom_components.vtherm_mpc_fan.data_collection import DataCollector, _HEADER


def _make_executor_hass() -> MagicMock:
    """Return a hass mock whose executor job runs inline during tests."""
    hass = MagicMock()

    async def run_in_executor(target, *args):
        return target(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=run_in_executor)
    return hass


@pytest.mark.asyncio
async def test_async_initialize_creates_header(tmp_path: Path) -> None:
    """The collector should create its CSV header via the executor."""
    hass = _make_executor_hass()
    collector = DataCollector(hass, str(tmp_path), "123456789")

    await collector.async_initialize()

    with open(collector.path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))

    assert rows == [_HEADER]
    hass.async_add_executor_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_record_appends_row(tmp_path: Path) -> None:
    """The collector should append a data row after initialization."""
    hass = _make_executor_hass()
    collector = DataCollector(hass, str(tmp_path), "123456789")

    await collector.async_initialize()
    await collector.async_record(
        hvac_mode="heat",
        current_temp=20.1234,
        target_temp=21.0,
        vtherm_slope=-0.2468,
        is_window_open=False,
        decision={
            "temperature_error": 0.8765,
            "projected_temperature": 20.5,
            "projected_temperature_error": 0.5,
            "minutes_since_last_change": 12.345,
            "current_fan": "low",
            "fan_mode": "medium",
            "reason": "Strong recovery",
        },
        phase="TRANSIENT",
        effective_slope=-0.2468,
        effective_timeout=15.4321,
        force=True,
        learning_ready=False,
        dead_time=10.987,
    )

    with open(collector.path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))

    assert len(rows) == 2
    assert rows[0] == _HEADER
    assert rows[1][0].endswith("Z")
    assert rows[1][1:] == [
        "heat",
        "20.123",
        "21.0",
        "0.876",
        "-0.2468",
        "-0.2468",
        "20.5",
        "0.5",
        "TRANSIENT",
        "12.35",
        "15.43",
        "low",
        "medium",
        "1",
        "Strong recovery",
        "0",
        "10.99",
        "0",
        "Not ready",
        "",
        "no",
        "",
        "",
        "",
        "",
        "0",
        "",
        "0",
        "0",
        "",
    ]
    assert hass.async_add_executor_job.await_count == 2


@pytest.mark.asyncio
async def test_async_record_handles_none_projected_values(tmp_path: Path) -> None:
    """None projected values should fallback without raising."""
    hass = _make_executor_hass()
    collector = DataCollector(hass, str(tmp_path), "123456789")

    await collector.async_initialize()
    await collector.async_record(
        hvac_mode="heat",
        current_temp=20.1234,
        target_temp=21.0,
        vtherm_slope=-0.2468,
        is_window_open=False,
        decision={
            "temperature_error": 0.8765,
            "projected_temperature": None,
            "projected_temperature_error": None,
            "minutes_since_last_change": 12.345,
            "current_fan": "low",
            "fan_mode": "medium",
            "reason": "Strong recovery",
        },
        phase="TRANSIENT",
        effective_slope=-0.2468,
        effective_timeout=15.4321,
        force=True,
        learning_ready=False,
        dead_time=10.987,
    )

    with open(collector.path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))

    assert len(rows) == 2
    assert rows[1][7:9] == ["20.123", "0.0"]
    assert hass.async_add_executor_job.await_count == 2


@pytest.mark.asyncio
async def test_async_initialize_rotates_file_on_header_change(tmp_path: Path) -> None:
    """The collector should rotate the active CSV when the schema changes."""
    legacy_path = tmp_path / "vtherm_mpc_fan_data_12345678.csv"
    rotated_path = tmp_path / "vtherm_mpc_fan_data_12345678_old.csv"
    legacy_path.write_text("timestamp,hvac_mode\n2026-01-01T00:00:00Z,heat\n", encoding="utf-8")

    hass = _make_executor_hass()
    collector = DataCollector(hass, str(tmp_path), "123456789")

    await collector.async_initialize()

    with open(collector.path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))

    assert rows == [_HEADER]
    assert rotated_path.exists()


@pytest.mark.asyncio
async def test_manager_initializes_the_data_collector() -> None:
    """The manager must await collector initialization before the first cycle."""
    hass = MagicMock()
    hass.data = {}
    hass.config = MagicMock()
    hass.config.config_dir = "/tmp"
    hass.config_entries.async_entries = MagicMock(return_value=[])
    hass.states.get = MagicMock(return_value=None)

    runtime = MagicMock()
    runtime.unique_id = "vtherm-uid"
    runtime.name = "Living room"
    runtime.cycle_min = 5
    runtime.underlying_fan_modes = ["low", "high"]

    fake_store = MagicMock()
    fake_store.async_load = AsyncMock(return_value=None)
    fake_store.async_save = AsyncMock()

    fake_collector = MagicMock()
    fake_collector.path = "/tmp/vtherm_mpc_fan_data_12345678.csv"
    fake_collector.async_initialize = AsyncMock()

    manager = MpcFanFeatureManager(runtime, hass)
    manager._config = MagicMock(return_value={CONF_DATA_COLLECTION: True})
    manager._entry_id = MagicMock(return_value="123456789")

    with patch("custom_components.vtherm_mpc_fan.manager.Store", return_value=fake_store):
        with patch(
            "custom_components.vtherm_mpc_fan.manager.DataCollector",
            return_value=fake_collector,
        ) as collector_cls:
            await manager.start_listening()

    collector_cls.assert_called_once_with(hass, hass.config.config_dir, "123456789")
    fake_collector.async_initialize.assert_awaited_once()


@pytest.mark.asyncio
async def test_manager_skips_the_collector_when_disabled() -> None:
    """With data collection off, no CSV file is opened at all."""
    hass = MagicMock()
    hass.data = {}
    hass.config.config_dir = "/tmp"
    hass.config_entries.async_entries = MagicMock(return_value=[])

    runtime = MagicMock()
    runtime.unique_id = "vtherm-uid"
    runtime.name = "Living room"
    runtime.cycle_min = 5
    runtime.underlying_fan_modes = ["low", "high"]

    fake_store = MagicMock()
    fake_store.async_load = AsyncMock(return_value=None)

    manager = MpcFanFeatureManager(runtime, hass)
    manager._config = MagicMock(return_value={CONF_DATA_COLLECTION: False})
    manager._entry_id = MagicMock(return_value="123456789")

    with patch("custom_components.vtherm_mpc_fan.manager.Store", return_value=fake_store):
        with patch(
            "custom_components.vtherm_mpc_fan.manager.DataCollector"
        ) as collector_cls:
            await manager.start_listening()

    collector_cls.assert_not_called()


def test_build_data_collection_decision_uses_live_cycle_metrics() -> None:
    """CSV audit payload should include the live error and elapsed minutes from the loop."""
    decision = _build_data_collection_decision(
        effective_fan="high",
        effective_reason="MPC: Strong recovery",
        current_fan="medium",
        current_error=0.8,
        minutes_since_change=14.5,
        hvac_mode="heat",
        target_temp=20.0,
        mpc_decision={},
    )

    assert decision["fan_mode"] == "high"
    assert decision["temperature_error"] == pytest.approx(0.8)
    assert decision["minutes_since_last_change"] == pytest.approx(14.5)
    assert decision["projected_temperature"] is None
    assert decision["projected_temperature_error"] is None


def test_build_data_collection_decision_uses_mpc_projection() -> None:
    """CSV audit payload should derive projected error from the MPC 10-minute temperature."""
    decision = _build_data_collection_decision(
        effective_fan="low",
        effective_reason="MPC paused",
        current_fan="low",
        current_error=0.2,
        minutes_since_change=6.0,
        hvac_mode="heat",
        target_temp=20.0,
        mpc_decision={"mpc_predicted_temperature_10m": 19.6},
    )

    assert decision["projected_temperature"] == 19.6
    assert decision["projected_temperature_error"] == pytest.approx(0.4)
