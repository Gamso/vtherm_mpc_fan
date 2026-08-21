"""Regression coverage for the stale-registry bug: after a config-entry reload,
every entity stayed "unavailable" forever.

Root cause: entity_bucket/add_entities_registry/managers live in hass.data and
were never cleared when Home Assistant actually tore an entry's entities down.
The next ensure_entities() call saw a non-empty bucket, concluded everything
already existed, and never re-added anything to the freshly republished
async_add_entities callback.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.vtherm_mpc_fan.manager import MpcFanFeatureManager
from custom_components.vtherm_mpc_fan.number import PLATFORM_NUMBER
from custom_components.vtherm_mpc_fan.registry import (
    add_entities_registry,
    clear_registry,
    entities_registry,
    managers,
)
from custom_components.vtherm_mpc_fan.sensor import PLATFORM_SENSOR

FAN_MODES = ["silent", "low", "med", "high", "superhigh"]


def test_clear_registry_drops_only_the_targeted_vtherm() -> None:
    """Scoped by unique_id: clearing one VTherm must not touch another's state."""
    hass = MagicMock()
    hass.data = {}

    managers(hass)["vtherm-a"] = MagicMock()
    managers(hass)["vtherm-b"] = MagicMock()
    add_entities_registry(hass)["vtherm-a"] = {"sensor": MagicMock()}
    add_entities_registry(hass)["vtherm-b"] = {"sensor": MagicMock()}
    entities_registry(hass)["vtherm-a"] = {"sensors": {"x": MagicMock()}}
    entities_registry(hass)["vtherm-b"] = {"sensors": {"y": MagicMock()}}

    clear_registry(hass, "vtherm-a")

    assert "vtherm-a" not in managers(hass)
    assert "vtherm-a" not in add_entities_registry(hass)
    assert "vtherm-a" not in entities_registry(hass)
    assert "vtherm-b" in managers(hass)
    assert "vtherm-b" in add_entities_registry(hass)
    assert "vtherm-b" in entities_registry(hass)


def test_clear_registry_is_a_no_op_for_an_unknown_id() -> None:
    """Nothing was ever registered for this id -- must not raise."""
    hass = MagicMock()
    hass.data = {}
    clear_registry(hass, "never-seen")  # must not raise


async def _loaded_manager(hass) -> MpcFanFeatureManager:
    """A manager whose model is loaded (mpc is set), entities not yet built."""
    runtime = MagicMock()
    runtime.unique_id = "vtherm-uid"
    runtime.entity_id = "climate.living_room"
    runtime.name = "Living room"
    runtime.cycle_min = 5
    runtime.underlying_fan_modes = list(FAN_MODES)

    manager = MpcFanFeatureManager(runtime, hass)
    manager._config = MagicMock(return_value={"data_collection": False})  # noqa: SLF001
    manager._entry_id = MagicMock(return_value="entry-1")  # noqa: SLF001

    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()
    with patch("custom_components.vtherm_mpc_fan.manager.Store", return_value=store):
        await manager._async_ensure_loaded()  # noqa: SLF001
    return manager


@pytest.mark.asyncio
async def test_reload_recreates_entities_instead_of_staying_unavailable() -> None:
    """The exact bug: populate the bucket, clear it as a real unload would, then
    confirm a second ensure_entities() (as a real reload's platform setup would
    trigger) rebuilds everything instead of silently doing nothing.
    """
    hass = MagicMock()
    hass.data = {}

    manager = await _loaded_manager(hass)

    # --- First "boot": platform publishes its callback, manager builds entities.
    first_add_sensor = MagicMock()
    first_add_number = MagicMock()
    registry = add_entities_registry(hass).setdefault(manager.vtherm_unique_id, {})
    registry[PLATFORM_SENSOR] = first_add_sensor
    registry[PLATFORM_NUMBER] = first_add_number
    manager.ensure_entities()

    assert first_add_sensor.call_args is not None
    assert len(first_add_sensor.call_args.args[0]) > 0
    assert first_add_number.call_args is not None
    assert len(first_add_number.call_args.args[0]) > 0

    # --- Config-entry reload: HA tears the entities down for real (simulated by
    # simply not touching them further) and the plugin's own async_unload_entry
    # clears this VTherm's registry -- the fix under test.
    clear_registry(hass, manager.vtherm_unique_id)

    # --- Platforms set up again and publish fresh callbacks (new AddEntitiesCallback
    # instances, as a real reload produces), then the manager rebuilds.
    second_add_sensor = MagicMock()
    second_add_number = MagicMock()
    registry = add_entities_registry(hass).setdefault(manager.vtherm_unique_id, {})
    registry[PLATFORM_SENSOR] = second_add_sensor
    registry[PLATFORM_NUMBER] = second_add_number
    manager.ensure_entities()

    # Without the fix, the bucket still "remembers" the first round's entities
    # and both of these calls never happen -- the bug this test exists for.
    second_add_sensor.assert_called_once()
    assert len(second_add_sensor.call_args.args[0]) > 0
    second_add_number.assert_called_once()
    assert len(second_add_number.call_args.args[0]) > 0


@pytest.mark.asyncio
async def test_without_clearing_the_registry_reload_would_stay_unavailable() -> None:
    """Sanity check on the test itself: skip the clear and reproduce the bug.

    Confirms the test above is actually exercising the failure mode, not just
    trivially passing regardless of clear_registry's effect.
    """
    hass = MagicMock()
    hass.data = {}
    manager = await _loaded_manager(hass)

    registry = add_entities_registry(hass).setdefault(manager.vtherm_unique_id, {})
    registry[PLATFORM_SENSOR] = MagicMock()
    registry[PLATFORM_NUMBER] = MagicMock()
    manager.ensure_entities()

    # New callbacks arrive (as on a real reload) but the bucket is NOT cleared.
    second_add_sensor = MagicMock()
    second_add_number = MagicMock()
    registry[PLATFORM_SENSOR] = second_add_sensor
    registry[PLATFORM_NUMBER] = second_add_number
    manager.ensure_entities()

    second_add_sensor.assert_not_called()
    second_add_number.assert_not_called()
