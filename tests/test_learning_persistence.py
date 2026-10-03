"""Tests for learning data persistence functionality."""
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry

from custom_components.vtherm_mpc_fan.manager import MpcFanFeatureManager
from custom_components.vtherm_mpc_fan.thermal_learning import ThermalLearning
from custom_components.vtherm_mpc_fan.const import (
    DOMAIN,
    CONF_DEADBAND,
    DEFAULT_DEADBAND,
    MIN_MODE_PROFILE_SAMPLES,
    MIN_SAMPLES_LEARNING,
)


class TestLearningPersistence:
    """Test learning data serialization and persistence."""

    def test_learning_to_dict_serialization(self):
        """Test that learning data can be serialized to dict."""
        learning = ThermalLearning()

        # Add some sample data
        learning.add_slope_sample("medium", 0.5, 0.3)
        learning.add_slope_sample("high", 0.8, 0.5)
        learning.add_response_event(12.5)

        # Serialize to dict
        data = learning.to_dict()

        # Verify structure
        assert isinstance(data, dict)
        assert "slope_samples" in data
        assert "response_events" in data
        assert "slope_count" in data
        assert "slope_mean" in data
        assert "slope_m2" in data
        assert "slope_max" in data

        # Verify data is preserved (at least partially for windowing)
        assert len(data["slope_samples"]) > 0
        assert len(data["response_events"]) > 0
        assert data["slope_count"] > 0

    def test_learning_from_dict_deserialization(self):
        """Test that learning data can be restored from dict."""
        # Create original learning instance with data
        original = ThermalLearning()
        original.add_slope_sample("medium", 0.5, 0.3)
        original.add_slope_sample("high", 0.8, 0.5)
        original.add_slope_sample("low", 0.3, 0.2)
        original.add_response_event(12.5)
        original.add_response_event(15.0)

        # Serialize
        data = original.to_dict()

        # Restore to new instance
        restored = ThermalLearning.from_dict(data)

        # Verify restoration
        assert restored.slope_count == original.slope_count
        assert restored.slope_mean == pytest.approx(original.slope_mean, rel=1e-5)
        assert restored.slope_max == pytest.approx(original.slope_max, rel=1e-5)
        assert len(restored.response_events) == len(original.response_events)

    def test_learning_empty_dict_initialization(self):
        """Test that learning can handle empty dict initialization."""
        restored = ThermalLearning.from_dict({})

        assert restored.slope_count == 0
        assert restored.slope_mean == 0.0
        assert restored.slope_max == 0.0
        assert len(restored.response_events) == 0
        assert not restored.is_ready()

    def test_learning_persistence_after_reset(self):
        """Test that reset clears all data including persistence data."""
        learning = ThermalLearning()

        # Add data
        learning.add_slope_sample("medium", 0.5, 0.3)
        learning.add_response_event(12.5)
        assert learning.slope_count > 0

        # Reset
        learning.reset()

        # Verify everything is cleared
        data = learning.to_dict()
        assert len(data["slope_samples"]) == 0
        assert len(data["response_events"]) == 0
        assert data["slope_count"] == 0
        assert data["slope_mean"] == 0.0
        assert data["slope_max"] == 0.0

    def test_learning_persistence_above_min_samples(self):
        """Test that a store holding exactly min_samples restores at 100% progress."""
        learning = ThermalLearning()
        assert learning.min_samples == MIN_SAMPLES_LEARNING

        # Exactly _min_samples (MIN_SAMPLES_LEARNING): the threshold for 100% progress
        for _ in range(MIN_SAMPLES_LEARNING):
            learning.add_slope_sample("medium", 0.5, 0.3)

        assert learning.slope_sample_count() == MIN_SAMPLES_LEARNING

        # Serialize and restore
        data = learning.to_dict()
        restored = ThermalLearning.from_dict(data)

        # Every sample must survive the round-trip
        assert len(data["slope_samples"]) == MIN_SAMPLES_LEARNING
        assert restored.slope_sample_count() == MIN_SAMPLES_LEARNING
        assert restored.get_progress() == 100.0
        assert restored.is_ready()

    def test_learning_sliding_window_cleanup(self):
        """Test that old samples are cleaned up from serialization."""
        learning = ThermalLearning()

        # Manually add old samples (simulate data from a week ago)
        days_in_seconds = 24 * 3600
        old_timestamp = time.time() - (8 * days_in_seconds)  # 8 days ago
        learning.slope_samples = [
            (old_timestamp, "medium", 0.5, "heat"),
            (time.time(), "high", 0.8, "heat"),
        ]
        learning.response_events = [
            (old_timestamp, 12.5),
            (time.time(), 15.0),
        ]

        # Serialize and restore (should clean up old data)
        data = learning.to_dict()
        restored = ThermalLearning.from_dict(data)

        # Old "medium" sample expired and has no within-window replacement,
        # but MIN_MODE_PROFILE_SAMPLES=10 means retention kicks in only when
        # a profile already has >= 10 samples.  With only 1 expired sample,
        # 1 is retained to preserve the profile's last known value.
        assert len(restored.slope_samples) == 2  # both kept (each profile < min)
        assert len(restored.response_events) == 1

    def test_learning_min_retention_preserves_rare_profile(self):
        """Old samples for a rarely-used profile are kept up to MIN_MODE_PROFILE_SAMPLES."""
        learning = ThermalLearning()
        days_in_seconds = 24 * 3600
        now = time.time()
        old = now - (8 * days_in_seconds)

        # Build sample lists directly and assign via public property
        expired_med = [(old + i, "med", 0.5, "heat") for i in range(10)]
        recent_med = [(now + i, "med", 0.4, "heat") for i in range(15)]
        expired_high = [(old + 100 + i, "high", 1.5, "heat") for i in range(8)]
        learning.slope_samples = expired_med + recent_med + expired_high

        cutoff = now - (7 * days_in_seconds)
        trimmed = ThermalLearning.trim_with_min_retention(learning.slope_samples, cutoff, MIN_MODE_PROFILE_SAMPLES)

        med_samples = [s for s in trimmed if s[1] == "med"]
        high_samples = [s for s in trimmed if s[1] == "high"]

        # med: 15 within-window, no shortfall → all 10 expired dropped
        assert len(med_samples) == 15
        # high: 0 within-window, shortfall=10, only 8 available → keep all 8
        assert len(high_samples) == 8

    @pytest.mark.asyncio
    async def test_manager_restores_the_model_from_storage(self):
        """The manager must rebuild its model from the Store before the first cycle."""
        current_time = time.time()
        stored = {
            "slope_samples": [(current_time - 3600, "medium", 0.5)],  # 1 hour ago
            "response_events": [(current_time - 3600, 12.5)],  # 1 hour ago
            "slope_count": 5,
            "slope_mean": 0.6,
            "slope_M2": 0.1,
            "slope_max": 0.9,
        }

        hass = MagicMock()
        hass.data = {}
        hass.config.config_dir = "/tmp"
        hass.config_entries.async_entries = MagicMock(return_value=[])

        runtime = MagicMock()
        runtime.unique_id = "vtherm-uid"
        runtime.name = "Living room"
        runtime.cycle_min = 5
        runtime.underlying_fan_modes = ["low", "medium", "high"]

        store = MagicMock()
        store.async_load = AsyncMock(return_value=stored)
        store.async_save = AsyncMock()

        manager = MpcFanFeatureManager(runtime, hass)
        manager._config = MagicMock(return_value={CONF_DEADBAND: DEFAULT_DEADBAND, "data_collection": False})
        manager._entry_id = MagicMock(return_value="test_entry_123")

        with patch("custom_components.vtherm_mpc_fan.manager.Store", return_value=store):
            await manager.start_listening()

        store.async_load.assert_awaited_once()
        # Timestamps are recent, so the sliding window keeps them.
        assert len(manager.learning.slope_samples) > 0
        assert len(manager.learning.response_events) > 0
        assert manager.mpc is not None

    @pytest.mark.asyncio
    async def test_manager_saves_the_model(self):
        """Persisting must round-trip through the same Store."""
        hass = MagicMock()
        hass.data = {}
        hass.config.config_dir = "/tmp"
        hass.config_entries.async_entries = MagicMock(return_value=[])

        runtime = MagicMock()
        runtime.unique_id = "vtherm-uid"
        runtime.name = "Living room"
        runtime.cycle_min = 5
        runtime.underlying_fan_modes = ["low", "high"]

        store = MagicMock()
        store.async_load = AsyncMock(return_value=None)
        store.async_save = AsyncMock()

        manager = MpcFanFeatureManager(runtime, hass)
        manager._config = MagicMock(return_value={"data_collection": False})
        manager._entry_id = MagicMock(return_value="entry-1")

        with patch("custom_components.vtherm_mpc_fan.manager.Store", return_value=store):
            await manager.start_listening()

        manager.learning.add_slope_sample("high", 0.8, 1.0, "heat")
        await manager.async_save()

        store.async_save.assert_awaited_once()
        saved = store.async_save.await_args.args[0]
        assert saved["slope_samples"]
