"""Tests for the factory VTherm asks, for every thermostat, whether to build a manager."""

from unittest.mock import MagicMock, PropertyMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vtherm_mpc_fan.const import (
    CONF_TARGET_VTHERM,
    CONF_THERMOSTAT_CLIMATE,
    CONF_THERMOSTAT_TYPE,
    DOMAIN,
    FEATURE_MANAGER_MPC_FAN,
)
from custom_components.vtherm_mpc_fan.factory import MpcFanManagerFactory
from custom_components.vtherm_mpc_fan.manager import MpcFanFeatureManager


def _thermostat(hass, unique_id: str = "vtherm-a", thermostat_type: str = CONF_THERMOSTAT_CLIMATE) -> MagicMock:
    """Return a VTherm runtime stand-in with the given type."""
    thermostat = MagicMock()
    thermostat.unique_id = unique_id
    thermostat.name = "Living room"
    thermostat.hass = hass
    thermostat.entry_infos = {CONF_THERMOSTAT_TYPE: thermostat_type}
    return thermostat


def _opt_in(hass, target: str = "vtherm-a") -> None:
    """Add a plugin entry targeting *target*."""
    MockConfigEntry(domain=DOMAIN, data={CONF_TARGET_VTHERM: target}).add_to_hass(hass)


def test_factory_is_named_after_the_feature_manager() -> None:
    """VThermAPI registers and unregisters factories by this name."""
    assert MpcFanManagerFactory(MagicMock()).name == FEATURE_MANAGER_MPC_FAN


async def test_factory_supports_an_opted_in_over_climate(integration) -> None:
    """An over_climate VTherm with a plugin entry gets a manager."""
    _opt_in(integration)
    assert MpcFanManagerFactory(integration).supports(_thermostat(integration)) is True


async def test_factory_ignores_a_vtherm_nobody_opted_in(integration) -> None:
    """An external plugin stays dormant on thermostats without an entry."""
    _opt_in(integration, target="vtherm-b")
    assert MpcFanManagerFactory(integration).supports(_thermostat(integration)) is False


async def test_factory_ignores_other_thermostat_types(integration) -> None:
    """Only over_climate proxies a fan-capable underlying."""
    _opt_in(integration)
    thermostat = _thermostat(integration, thermostat_type="thermostat_over_switch")
    assert MpcFanManagerFactory(integration).supports(thermostat) is False


@pytest.mark.parametrize(("fan_modes", "expected"), [(None, False), (["low"], True)])
async def test_factory_falls_back_to_fan_modes_without_a_raw_config(integration, fan_modes, expected) -> None:
    """A runtime exposing no config dict is judged on whether it proxies fan modes."""
    _opt_in(integration)
    thermostat = _thermostat(integration)
    thermostat.entry_infos = object()
    thermostat.underlying_fan_modes = fan_modes
    assert MpcFanManagerFactory(integration).supports(thermostat) is expected


async def test_factory_survives_a_runtime_whose_config_raises(integration) -> None:
    """A half-initialised thermostat must not break VTherm's own setup."""
    _opt_in(integration)
    thermostat = _thermostat(integration)
    type(thermostat).entry_infos = PropertyMock(side_effect=RuntimeError("not ready"))
    assert MpcFanManagerFactory(integration).supports(thermostat) is False


async def test_factory_creates_a_manager_bound_to_the_thermostat(integration) -> None:
    """create() hands VTherm a manager wired to that thermostat and its hass."""
    thermostat = _thermostat(integration)

    manager = MpcFanManagerFactory(integration).create(thermostat)

    assert isinstance(manager, MpcFanFeatureManager)
    assert manager.vtherm is thermostat
    assert manager.hass is integration
