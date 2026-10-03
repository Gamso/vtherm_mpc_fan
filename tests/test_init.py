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

    await hass.services.async_call(
        DOMAIN, SERVICE_SET_EFFECTIVE_SLOPE, {"hvac_mode": "cool", "fan_mode": "low", "effective_slope": 0.4}, blocking=True
    )

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
