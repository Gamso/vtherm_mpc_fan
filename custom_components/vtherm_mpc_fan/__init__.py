"""The vtherm_mpc_fan integration.

An MPC-based fan-speed controller for Versatile Thermostat, packaged as an
external Feature Manager plugin. This module only wires the plugin into VTherm:
it registers the factory, sets up the entity platforms and exposes the services.
The control cycle lives in :mod:`manager`, the decision logic in
:mod:`mpc_controller` and the learned model in :mod:`thermal_learning`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .log import get_logger, write_event_log
from .const import (
    CONF_TARGET_VTHERM,
    CONF_THERMOSTAT_CLIMATE,
    CONF_THERMOSTAT_TYPE,
    DATA_FACTORY_REGISTERED,
    DOMAIN,
    FEATURE_MANAGER_MPC_FAN,
    PROFILE_HVAC_MODES,
    VTHERM_DOMAIN,
)
from .factory import MpcFanManagerFactory
from .manager import FanOverride
from .registry import clear_registry, domain_data, managers

_LOGGER = get_logger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.NUMBER]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)  # pylint: disable=invalid-name  # name required by HA

SERVICE_RESET_LEARNING = "reset_learning"
SERVICE_SET_EFFECTIVE_SLOPE = "set_effective_slope"
SERVICE_FORCE_FAN = "force_fan"

ATTR_TARGET_VTHERM = "target_vtherm"

# Service bounds. services.yaml only feeds the UI form; Home Assistant validates
# a call against the voluptuous schema alone, so a script or automation could
# otherwise store an absurd slope (kept until reset_learning) or a 1e12-minute
# override. Same limits as services.yaml and the effective-slope number entity.
EFFECTIVE_SLOPE_MIN = -2.0
EFFECTIVE_SLOPE_MAX = 5.0
FORCE_FAN_MAX_MINUTES = 1440


def _register_factory(hass: HomeAssistant) -> bool:
    """Register the MPC fan factory with the shared VTherm API."""
    data = domain_data(hass)
    if data.get(DATA_FACTORY_REGISTERED) is True:
        return True

    # Imported here, not at module scope: vtherm_api ships with Versatile
    # Thermostat rather than being declared in this manifest, so on an install
    # without VTherm a top-level import would fail while Home Assistant is
    # loading the integration -- surfacing as an unhelpful traceback instead of
    # the actionable message below.
    try:
        from vtherm_api.vtherm_api import VThermAPI  # pylint: disable=import-outside-toplevel
    except ImportError:
        _LOGGER.error("vtherm_api is not available. Install Versatile Thermostat >= 10.2.0, which provides it")
        return False

    api = VThermAPI.get_vtherm_api(hass)
    if api is None:
        _LOGGER.warning("VThermAPI unavailable; the MPC fan factory is not registered yet")
        return False

    factory = MpcFanManagerFactory(hass)
    if api.get_feature_manager(factory.name) is None:
        api.register_feature_manager(factory)

    data[DATA_FACTORY_REGISTERED] = True
    return True


def _unregister_factory(hass: HomeAssistant) -> None:
    """Unregister the factory from the shared VTherm API."""
    # Local for the same reason as in _register_factory; here a missing
    # vtherm_api simply means there is nothing registered to undo.
    try:
        from vtherm_api.vtherm_api import VThermAPI  # pylint: disable=import-outside-toplevel
    except ImportError:
        return
    api = VThermAPI.get_vtherm_api(hass)
    if api is not None:
        api.unregister_feature_manager(FEATURE_MANAGER_MPC_FAN)
    domain_data(hass)[DATA_FACTORY_REGISTERED] = False


async def _reload_target_vtherms(hass: HomeAssistant, source_entry: ConfigEntry | None = None) -> None:
    """Reload the over_climate VTherms so they pick the manager up.

    A VTherm builds its feature managers at setup, so a plugin registered or
    removed afterwards only takes effect once the thermostat is reloaded.
    """
    target = source_entry.data.get(CONF_TARGET_VTHERM) if source_entry is not None else None

    reload_tasks = []
    for entry in hass.config_entries.async_entries(VTHERM_DOMAIN):
        if entry.data.get(CONF_THERMOSTAT_TYPE) != CONF_THERMOSTAT_CLIMATE:
            continue
        # Matched on entry_id, not on entry.unique_id: VTherm never calls
        # async_set_unique_id, so its config entries have unique_id None, while
        # the id this plugin stores is the *thermostat's* unique_id -- which
        # VTherm derives as `entry.entry_id` (see its climate.py). Comparing
        # against entry.unique_id silently matched nothing, so the targeted
        # reload did no work and the feature manager only appeared after a full
        # Home Assistant restart.
        if target is not None and entry.entry_id != target:
            continue
        reload_tasks.append(hass.config_entries.async_reload(entry.entry_id))

    if reload_tasks:
        await asyncio.gather(*reload_tasks)


def _resolve_manager(hass: HomeAssistant, target: str | None):
    """Return the manager for *target*, or the only one when target is omitted."""
    live = managers(hass)
    if not live:
        raise HomeAssistantError("No VTherm MPC Fan manager is running. Check that the target VTherm is an over_climate thermostat and has been reloaded.")

    if target:
        manager = live.get(target)
        if manager is not None:
            return manager
        for candidate in live.values():
            if target in (candidate.vtherm.entity_id, candidate.vtherm_name):
                return candidate
        raise HomeAssistantError(f"No VTherm MPC Fan manager found for '{target}'. Known: {', '.join(m.vtherm.entity_id for m in live.values())}")

    if len(live) > 1:
        raise HomeAssistantError(f"Several VTherms are managed; pass '{ATTR_TARGET_VTHERM}' to pick one: {', '.join(m.vtherm.entity_id for m in live.values())}")
    return next(iter(live.values()))


def _register_services(hass: HomeAssistant) -> None:
    """Register the domain services once per Home Assistant instance."""

    async def reset_learning(call):
        """Clear the learned model and start over."""
        manager = _resolve_manager(hass, call.data.get(ATTR_TARGET_VTHERM))
        manager.learning.reset()
        await manager.async_save()
        _LOGGER.info("Learning reset for %s", manager.vtherm_name)

    async def set_effective_slope(call):
        """Set the effective slope of one fan/HVAC profile by hand."""
        manager = _resolve_manager(hass, call.data.get(ATTR_TARGET_VTHERM))
        hvac_mode = call.data["hvac_mode"]
        fan_mode = call.data["fan_mode"]
        effective_slope = float(call.data["effective_slope"])

        manager.learning.set_mode_effective_slope(fan_mode, hvac_mode, effective_slope)
        await manager.async_save()
        _LOGGER.info(
            "Set effective slope for %s %s/%s to %.3f",
            manager.vtherm_name,
            hvac_mode,
            fan_mode,
            effective_slope,
        )

    async def force_fan(call):
        """Pin a fan mode for a fixed duration; 0 minutes cancels the override."""
        manager = _resolve_manager(hass, call.data.get(ATTR_TARGET_VTHERM))
        fan_mode = call.data["fan_mode"]
        duration_minutes = float(call.data["duration_minutes"])

        if duration_minutes <= 0:
            manager.force = None
            write_event_log(_LOGGER, manager, "force_fan override cancelled, resuming MPC control")
        else:
            available = manager.fan_modes or []
            if available and fan_mode not in available:
                raise HomeAssistantError(f"Fan mode '{fan_mode}' is not available for {manager.vtherm_name}. Known fan modes: {', '.join(available) or 'none yet'}")
            manager.force = FanOverride(
                fan_mode=fan_mode,
                until=time.time() + duration_minutes * 60.0,
            )
            write_event_log(_LOGGER, manager, f"force_fan override: '{fan_mode}' for {duration_minutes:.0f} min")

        # Apply now rather than waiting for the next VTherm cycle.
        await manager.refresh_state()

    optional_target = {vol.Optional(ATTR_TARGET_VTHERM): cv.string}

    hass.services.async_register(DOMAIN, SERVICE_RESET_LEARNING, reset_learning, schema=vol.Schema(optional_target))
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_EFFECTIVE_SLOPE,
        set_effective_slope,
        schema=vol.Schema(
            {
                **optional_target,
                vol.Required("hvac_mode"): vol.In(PROFILE_HVAC_MODES),
                vol.Required("fan_mode"): cv.string,
                vol.Required("effective_slope"): vol.All(
                    vol.Coerce(float),
                    vol.Range(min=EFFECTIVE_SLOPE_MIN, max=EFFECTIVE_SLOPE_MAX),
                ),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_FAN,
        force_fan,
        schema=vol.Schema(
            {
                **optional_target,
                vol.Required("fan_mode"): cv.string,
                vol.Required("duration_minutes"): vol.All(vol.Coerce(float), vol.Range(min=0, max=FORCE_FAN_MAX_MINUTES)),
            }
        ),
    )


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up vtherm_mpc_fan from YAML."""
    del config
    _register_factory(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up vtherm_mpc_fan from a config entry."""
    domain_data(hass)[entry.entry_id] = entry.entry_id
    _register_factory(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, SERVICE_RESET_LEARNING):
        _register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    # At initial startup VTherm sets its own entries up independently; reloading
    # here would race with it.
    if hass.state == CoreState.running:
        await _reload_target_vtherms(hass, entry)

    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry so changed options reach the manager."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a vtherm_mpc_fan config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # async_unload_platforms just tore down this VTherm's sensor/number entity
    # objects for real -- this is the one moment that is unambiguous, unlike
    # MpcFanFeatureManager.stop_listening(), which VTherm also calls when it is
    # merely restarting the *same* manager instance without actually removing
    # its entities (so clearing there would wrongly nuke a still-valid bucket
    # and cause ensure_entities() to re-add duplicates of live entities).
    # Skipping this leaves clear_registry's target dict pointing at objects HA
    # already destroyed: the next ensure_entities() sees a non-empty bucket,
    # assumes everything already exists, and never re-adds anything to the
    # fresh async_add_entities callback the reloaded platform just published --
    # every entity stays "unavailable" until a full HA restart. This is exactly
    # what happens on every config-entry reload (e.g. editing options), since
    # unregistering/re-registering the factory each forces a VTherm reload.
    target = entry.data.get(CONF_TARGET_VTHERM)
    if target:
        clear_registry(hass, target)

    data = domain_data(hass)
    data.pop(entry.entry_id, None)

    remaining = [key for key in data if not key.startswith(("factory_", "managers", "add_entities", "entities"))]
    if not remaining:
        _unregister_factory(hass)
        for service in (
            SERVICE_RESET_LEARNING,
            SERVICE_SET_EFFECTIVE_SLOPE,
            SERVICE_FORCE_FAN,
        ):
            hass.services.async_remove(DOMAIN, service)
        # Only the VTherm this entry drove still holds a manager to drop; every
        # other over_climate thermostat never had one and needs no reload.
        await _reload_target_vtherms(hass, entry)

    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload a vtherm_mpc_fan config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
