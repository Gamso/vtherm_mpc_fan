"""Integration-level tests of __init__.py: setup, services and unload."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from vtherm_api.vtherm_api import VThermAPI

from custom_components.vtherm_mpc_fan import (
    SERVICE_APPLY_LEARNED_SETTINGS,
    SERVICE_FORCE_FAN,
    SERVICE_RESET_LEARNING,
    SERVICE_SET_EFFECTIVE_SLOPE,
    _resolve_manager,
)
from custom_components.vtherm_mpc_fan.const import (
    CONF_TARGET_VTHERM,
    CONF_THERMOSTAT_CLIMATE,
    CONF_THERMOSTAT_TYPE,
    DOMAIN,
    FEATURE_MANAGER_MPC_FAN,
    VTHERM_DOMAIN,
)
from custom_components.vtherm_mpc_fan.registry import managers
from custom_components.vtherm_mpc_fan.thermal_learning import ThermalLearning

SERVICES = (SERVICE_APPLY_LEARNED_SETTINGS, SERVICE_RESET_LEARNING, SERVICE_SET_EFFECTIVE_SLOPE, SERVICE_FORCE_FAN)


def _vtherm_entry(hass, entry_id: str, thermostat_type: str = CONF_THERMOSTAT_CLIMATE) -> MockConfigEntry:
    """Add a VTherm config entry; VTherm uses its entry_id as the thermostat unique_id."""
    entry = MockConfigEntry(domain=VTHERM_DOMAIN, entry_id=entry_id, data={CONF_THERMOSTAT_TYPE: thermostat_type})
    entry.add_to_hass(hass)
    return entry


def _plugin_entry(hass, target: str) -> MockConfigEntry:
    """Add a vtherm_mpc_fan config entry targeting the VTherm *target*."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=f"{DOMAIN}-{target}", data={CONF_TARGET_VTHERM: target})
    entry.add_to_hass(hass)
    return entry


def _fake_manager(unique_id: str, entity_id: str, name: str) -> MagicMock:
    """Return a manager stand-in as the services see it."""
    manager = MagicMock()
    manager.vtherm_unique_id = unique_id
    manager.vtherm.entity_id = entity_id
    manager.vtherm_name = name
    manager.learning = ThermalLearning()
    manager.fan_modes = ["low", "high"]
    manager.force = None
    manager.async_save = AsyncMock()
    manager.refresh_state = AsyncMock()
    return manager


@pytest.fixture
def reload_spy(integration):
    """Replace config-entry reloads with a recorder: VTherm itself is not installed."""
    spy = AsyncMock(return_value=True)
    with patch.object(integration.config_entries, "async_reload", spy):
        yield spy


async def _setup(hass, target: str = "vtherm-a") -> MockConfigEntry:
    """Set up one plugin entry and return it."""
    entry = _plugin_entry(hass, target)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_registers_the_factory_and_the_services(integration, reload_spy) -> None:
    """Setting an entry up publishes the factory to VThermAPI and the four services."""
    hass = integration
    _vtherm_entry(hass, "vtherm-a")

    await _setup(hass)

    assert VThermAPI.get_vtherm_api(hass).get_feature_manager(FEATURE_MANAGER_MPC_FAN) is not None
    for service in SERVICES:
        assert hass.services.has_service(DOMAIN, service)
    # A running instance reloads the targeted VTherm so it builds the manager.
    reload_spy.assert_awaited_once_with("vtherm-a")


# --- S-1: service schemas are bounded ----------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        {"hvac_mode": "heat", "fan_mode": "low", "effective_slope": 1e9},
        {"hvac_mode": "heat", "fan_mode": "low", "effective_slope": -2.5},
        {"hvac_mode": "dry", "fan_mode": "low", "effective_slope": 0.5},
    ],
)
async def test_set_effective_slope_rejects_out_of_range_values(integration, reload_spy, data) -> None:
    """The UI bounds are now enforced by the schema, so a script cannot store 1e9."""
    hass = integration
    await _setup(hass)
    manager = _fake_manager("vtherm-a", "climate.a", "A")
    managers(hass)["vtherm-a"] = manager

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, SERVICE_SET_EFFECTIVE_SLOPE, data, blocking=True)
    assert manager.learning.slope_sample_count() == 0


async def test_set_effective_slope_accepts_a_value_in_range(integration, reload_spy) -> None:
    """A bounded value still reaches the model and is persisted."""
    hass = integration
    await _setup(hass)
    manager = _fake_manager("vtherm-a", "climate.a", "A")
    managers(hass)["vtherm-a"] = manager

    await hass.services.async_call(DOMAIN, SERVICE_SET_EFFECTIVE_SLOPE, {"hvac_mode": "cool", "fan_mode": "low", "effective_slope": 0.4}, blocking=True)

    assert manager.learning.get_mode_effective_slope("low", "cool") == pytest.approx(0.4)
    manager.async_save.assert_awaited_once()


@pytest.mark.parametrize("minutes", [1e12, 1441, -5])
async def test_force_fan_rejects_out_of_range_durations(integration, reload_spy, minutes) -> None:
    """A day is the longest override; anything else is a mistyped automation."""
    hass = integration
    await _setup(hass)
    manager = _fake_manager("vtherm-a", "climate.a", "A")
    managers(hass)["vtherm-a"] = manager

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, SERVICE_FORCE_FAN, {"fan_mode": "high", "duration_minutes": minutes}, blocking=True)
    assert manager.force is None


# --- Unload ---------------------------------------------------------------


async def test_unloading_the_last_entry_reloads_only_its_own_vtherm(integration, reload_spy) -> None:
    """Removing the plugin must not reload every over_climate VTherm of the house."""
    hass = integration
    _vtherm_entry(hass, "vtherm-a")
    _vtherm_entry(hass, "vtherm-b")
    _vtherm_entry(hass, "vtherm-c", thermostat_type="thermostat_over_switch")
    entry = await _setup(hass)
    reload_spy.reset_mock()

    assert await hass.config_entries.async_unload(entry.entry_id)

    reload_spy.assert_awaited_once_with("vtherm-a")


async def test_unloading_the_last_entry_tears_everything_down(integration, reload_spy) -> None:
    """Services, the VThermAPI factory and the registry slot all go with the last entry."""
    hass = integration
    entry = await _setup(hass)
    managers(hass)["vtherm-a"] = _fake_manager("vtherm-a", "climate.a", "A")

    assert await hass.config_entries.async_unload(entry.entry_id)

    for service in SERVICES:
        assert not hass.services.has_service(DOMAIN, service)
    assert VThermAPI.get_vtherm_api(hass).get_feature_manager(FEATURE_MANAGER_MPC_FAN) is None
    assert "vtherm-a" not in managers(hass)


async def test_unloading_one_of_two_entries_keeps_the_services(integration, reload_spy) -> None:
    """The services and the factory are shared: they stay while another entry runs."""
    hass = integration
    first = await _setup(hass, "vtherm-a")
    await _setup(hass, "vtherm-b")

    assert await hass.config_entries.async_unload(first.entry_id)

    for service in SERVICES:
        assert hass.services.has_service(DOMAIN, service)
    assert VThermAPI.get_vtherm_api(hass).get_feature_manager(FEATURE_MANAGER_MPC_FAN) is not None


# --- _resolve_manager ---------------------------------------------------------


async def test_resolve_manager_without_any_manager_explains_why(integration) -> None:
    """No running manager is an actionable error, not a KeyError."""
    with pytest.raises(HomeAssistantError, match="No VTherm MPC Fan manager is running"):
        _resolve_manager(integration, None)


async def test_resolve_manager_defaults_to_the_only_one(integration) -> None:
    """With a single managed VTherm the target may be omitted."""
    only = _fake_manager("vtherm-a", "climate.a", "A")
    managers(integration)["vtherm-a"] = only
    assert _resolve_manager(integration, None) is only


@pytest.mark.parametrize("target", ["vtherm-b", "climate.b", "B"])
async def test_resolve_manager_accepts_unique_id_entity_id_or_name(integration, target) -> None:
    """Services take whichever identifier the user has at hand."""
    managers(integration)["vtherm-a"] = _fake_manager("vtherm-a", "climate.a", "A")
    wanted = managers(integration)["vtherm-b"] = _fake_manager("vtherm-b", "climate.b", "B")
    assert _resolve_manager(integration, target) is wanted


async def test_resolve_manager_requires_a_target_when_several_run(integration) -> None:
    """Guessing between two thermostats would act on the wrong room."""
    managers(integration)["vtherm-a"] = _fake_manager("vtherm-a", "climate.a", "A")
    managers(integration)["vtherm-b"] = _fake_manager("vtherm-b", "climate.b", "B")
    with pytest.raises(HomeAssistantError, match="Several VTherms"):
        _resolve_manager(integration, None)
    with pytest.raises(HomeAssistantError, match="climate.a, climate.b"):
        _resolve_manager(integration, "climate.nowhere")


# --- Services, nominal paths ----------------------------------------------------


async def test_force_fan_sets_the_override_and_runs_a_cycle(integration, reload_spy) -> None:
    """The override is stored with its deadline and applied without waiting for VTherm."""
    import time

    hass = integration
    await _setup(hass)
    manager = managers(hass)["vtherm-a"] = _fake_manager("vtherm-a", "climate.a", "A")

    await hass.services.async_call(DOMAIN, SERVICE_FORCE_FAN, {"fan_mode": "high", "duration_minutes": 30}, blocking=True)

    assert manager.force.fan_mode == "high"
    assert manager.force.until == pytest.approx(time.time() + 1800, abs=5)
    manager.refresh_state.assert_awaited_once()


async def test_force_fan_zero_minutes_cancels(integration, reload_spy) -> None:
    """Duration 0 hands control back to the MPC."""
    hass = integration
    await _setup(hass)
    manager = managers(hass)["vtherm-a"] = _fake_manager("vtherm-a", "climate.a", "A")
    manager.force = MagicMock()

    await hass.services.async_call(DOMAIN, SERVICE_FORCE_FAN, {"fan_mode": "high", "duration_minutes": 0}, blocking=True)

    assert manager.force is None


async def test_force_fan_refuses_a_speed_the_unit_does_not_have(integration, reload_spy) -> None:
    """An unknown speed fails loudly instead of being sent to the device."""
    hass = integration
    await _setup(hass)
    manager = managers(hass)["vtherm-a"] = _fake_manager("vtherm-a", "climate.a", "A")

    with pytest.raises(HomeAssistantError, match="turbo"):
        await hass.services.async_call(DOMAIN, SERVICE_FORCE_FAN, {"fan_mode": "turbo", "duration_minutes": 10}, blocking=True)
    assert manager.force is None


async def test_reset_learning_clears_and_persists(integration, reload_spy) -> None:
    """reset_learning empties the model and saves the empty state."""
    hass = integration
    await _setup(hass)
    manager = managers(hass)["vtherm-a"] = _fake_manager("vtherm-a", "climate.a", "A")
    manager.learning.add_slope_sample("low", 0.4, 0.5, "heat")

    await hass.services.async_call(DOMAIN, SERVICE_RESET_LEARNING, {}, blocking=True)

    assert manager.learning.slope_sample_count() == 0
    manager.async_save.assert_awaited_once()


async def test_apply_learned_settings_only_logs_before_readiness(integration, reload_spy, caplog) -> None:
    """Nothing is changed: the service reports progress until learning is ready."""
    hass = integration
    await _setup(hass)
    managers(hass)["vtherm-a"] = _fake_manager("vtherm-a", "climate.a", "A")

    await hass.services.async_call(DOMAIN, SERVICE_APPLY_LEARNED_SETTINGS, {}, blocking=True)

    assert "Learning not complete yet" in caplog.text
