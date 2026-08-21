"""Shared accessors for the plugin registries stored in ``hass.data``.

A feature-manager plugin does not own its own control loop or entity lifecycle:
VTherm creates one manager per eligible thermostat, while Home Assistant sets up
the entity platforms independently and in no guaranteed order. These helpers give
both sides a single decoupled rendez-vous point, keyed by the VTherm
``unique_id``, so whichever arrives second can pick up what the first published.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .const import (
    CONFLICTING_FAN_PLUGINS,
    DATA_ADD_ENTITIES,
    DATA_ENTITIES,
    DATA_MANAGERS,
    DOMAIN,
)

if TYPE_CHECKING:
    from .manager import MpcFanFeatureManager


def domain_data(hass: HomeAssistant) -> dict[str, Any]:
    """Return the plugin storage in hass.data, creating it if needed."""
    return hass.data.setdefault(DOMAIN, {})


def managers(hass: HomeAssistant) -> dict[str, "MpcFanFeatureManager"]:
    """Return the live managers keyed by VTherm unique_id."""
    return domain_data(hass).setdefault(DATA_MANAGERS, {})


def get_manager(hass: HomeAssistant, unique_id: str) -> "MpcFanFeatureManager | None":
    """Return the manager bound to a VTherm unique_id, if any."""
    return managers(hass).get(unique_id)


def add_entities_registry(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Return the ``async_add_entities`` callbacks by VTherm unique_id."""
    return domain_data(hass).setdefault(DATA_ADD_ENTITIES, {})


def entities_registry(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Return the created entities by VTherm unique_id."""
    return domain_data(hass).setdefault(DATA_ENTITIES, {})


def entity_bucket(hass: HomeAssistant, unique_id: str) -> dict[str, Any]:
    """Return the entity bucket for a VTherm unique_id, creating it if needed."""
    return entities_registry(hass).setdefault(unique_id, {"sensors": {}, "profiles": {}})


def clear_registry(hass: HomeAssistant, unique_id: str) -> None:
    """Drop every stored reference for one VTherm: its manager, its platform
    callbacks, and its entity bucket.

    Call this only from ``async_unload_entry``, after
    ``async_unload_platforms`` has actually torn down the entities -- not from
    ``MpcFanFeatureManager.stop_listening()``. VTherm also calls
    ``stop_listening`` when it is merely restarting the *same* manager
    instance without removing its entities, and clearing there would nuke a
    still-valid bucket, making ``ensure_entities()`` re-add duplicates of live
    entities. Skipping this call on real unload leaves the bucket pointing at
    entity objects Home Assistant already destroyed, so the next
    ``ensure_entities()`` sees a non-empty bucket, concludes everything
    already exists, and never re-adds anything to the fresh
    ``async_add_entities`` callback the reloaded platform just published --
    every entity stays permanently "unavailable" until a full HA restart.
    """
    managers(hass).pop(unique_id, None)
    add_entities_registry(hass).pop(unique_id, None)
    entities_registry(hass).pop(unique_id, None)


def find_conflicting_plugin(hass: HomeAssistant, vtherm_unique_id: str) -> str | None:
    """Return the domain of another fan-driving plugin on this VTherm, else None.

    Only one controller may own a fan. Two of them do not merely duplicate work:
    each reads the other's command as an external change, the speed flaps between
    their two opinions, and both learn from a trajectory neither produced -- which
    corrupts the learned model on top of the visible churn.
    """
    config_entries = getattr(hass, "config_entries", None)
    if config_entries is None:
        return None

    for domain, target_key in CONFLICTING_FAN_PLUGINS.items():
        try:
            entries = config_entries.async_entries(domain)
        except Exception:  # pylint: disable=broad-except
            continue
        for entry in entries:
            if entry.data.get(target_key) == vtherm_unique_id:
                return domain
    return None
