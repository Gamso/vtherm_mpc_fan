"""Number platform for VTherm MPC Fan: editable per-profile effective slope.

An entity's value being learned automatically doesn't mean it should be
read-only: a ``sensor`` can only be edited via the ``set_effective_slope``
service (Developer Tools -> Services), which is fine for automations but is a
clunky way for a person to just fix one wrong value. ``vtherm_auto_fan_extended``
faces the same choice for its thresholds and resolves it the same way -- a
``number`` entity, editable by clicking/dragging in the UI, that writes through
to the same underlying call the service makes.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .const import (
    CONF_TARGET_VTHERM,
    DEVICE_NAME,
    DOMAIN,
    EFFECTIVE_SLOPE_UNIT,
    MIN_MODE_PROFILE_SAMPLES,
    PROFILE_HVAC_MODES,
    REFERENCE_SLOPE_ERROR,
    build_scoped_entity_id,
    build_unique_id,
)
from .registry import add_entities_registry, entity_bucket, get_manager

PLATFORM_NUMBER = "number"

_LOGGER = logging.getLogger(__name__)

# Matches the bounds the set_effective_slope service already exposes, so a
# value set through either path means the same thing.
NATIVE_MIN_VALUE = -2.0
NATIVE_MAX_VALUE = 5.0
NATIVE_STEP = 0.001


def profile_effective_slope_object_key(hvac_mode: str, fan_mode: str) -> str:
    """Return the canonical object key for a profile effective-slope entity."""
    return f"{hvac_mode}_{slugify(fan_mode)}_effective_slope"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Publish the platform callback and build the numbers if the manager is up.

    Mirrors sensor.py's rendez-vous: Home Assistant sets this platform up on its
    own schedule, independent of when VTherm creates the feature manager, so
    whichever side arrives second does the work -- here, or from
    ``MpcFanFeatureManager.ensure_entities()`` on its first cycle. Merged into
    the registry entry rather than replacing it: sensor.py's own
    async_setup_entry writes to the same dict, on its own schedule, and a plain
    assignment here would erase its callback if it happened to run first.
    """
    target_unique_id = entry.data.get(CONF_TARGET_VTHERM)
    if not target_unique_id:
        _LOGGER.error("Config entry %s has no target VTherm; no number created", entry.entry_id)
        return

    registry = add_entities_registry(hass).setdefault(target_unique_id, {})
    registry[PLATFORM_NUMBER] = async_add_entities
    registry.setdefault("entry_id", entry.entry_id)

    manager = get_manager(hass, target_unique_id)
    if manager is not None:
        manager.ensure_entities()


def build_entities(manager) -> list[NumberEntity]:
    """Create one effective-slope number per (hvac_mode, fan_mode) not yet built.

    Safe to call every cycle: the fan-mode ladder can grow after startup, and
    new profile entities must follow it. ``bucket["profiles"]`` is shared with
    what used to be the profile *sensor* platform's storage -- there is only
    ever one per-profile entity, now a number instead of a sensor.
    """
    hass = manager.hass
    bucket = entity_bucket(hass, manager.vtherm_unique_id)
    registry = add_entities_registry(hass).get(manager.vtherm_unique_id) or {}
    entry_id = registry.get("entry_id", manager.vtherm_unique_id)
    climate_entity = manager.vtherm.entity_id
    if manager.mpc is None:
        return []

    known_keys = set(bucket["profiles"])
    new_entities: list[NumberEntity] = []
    for hvac_mode in PROFILE_HVAC_MODES:
        for fan_mode in manager.fan_modes or []:
            key = (hvac_mode, fan_mode)
            if key in known_keys:
                continue
            known_keys.add(key)
            entity = EffectiveSlopeNumber(
                entry_id,
                climate_entity,
                manager.mpc,
                hvac_mode,
                fan_mode,
                on_change=manager.async_save,
            )
            bucket["profiles"][key] = entity
            new_entities.append(entity)

    return new_entities


class EffectiveSlopeNumber(NumberEntity):
    """Editable effective slope for one (hvac_mode, fan_mode) profile.

    Reads live rather than caching: both the learned model and the live
    rank-scaled fallback (see ``MPCController.get_live_mode_slope``) can change
    between the periodic pushes the manager triggers, and there is no cheaper
    correct alternative to asking the model directly.
    """

    _attr_has_entity_name = True
    _attr_native_min_value = NATIVE_MIN_VALUE
    _attr_native_max_value = NATIVE_MAX_VALUE
    _attr_native_step = NATIVE_STEP
    _attr_native_unit_of_measurement = EFFECTIVE_SLOPE_UNIT
    _attr_mode = NumberMode.BOX  # a slider over a 7-wide range at 0.001 steps is unusable
    _attr_icon = "mdi:chart-line-variant"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        entry_id: str,
        climate_entity: str,
        controller,
        hvac_mode: str,
        fan_mode: str,
        *,
        on_change: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self._entry_id = entry_id
        self._climate_entity = climate_entity
        self._controller = controller
        self._hvac_mode = hvac_mode
        self._fan_mode = fan_mode
        # Optional so a standalone controller (e.g. in tests) can construct
        # this entity without a full manager to persist through.
        self._on_change = on_change

        object_key = profile_effective_slope_object_key(hvac_mode, fan_mode)
        self._attr_unique_id = build_unique_id(object_key, entry_id)
        self.entity_id = build_scoped_entity_id(PLATFORM_NUMBER, climate_entity, object_key)
        self._attr_name = f"{hvac_mode.title()} {fan_mode.title()} Effective Slope"

    @property
    def device_info(self) -> DeviceInfo:
        """Link the entity to the VTherm MPC Fan device."""
        return DeviceInfo(identifiers={(DOMAIN, self._entry_id)}, name=DEVICE_NAME)

    @property
    def hvac_mode(self) -> str:
        """The HVAC mode this profile belongs to."""
        return self._hvac_mode

    @property
    def fan_mode(self) -> str:
        """The fan mode this profile belongs to."""
        return self._fan_mode

    def _sample_count(self) -> int:
        return self._controller.learning.get_mode_sample_count(self._fan_mode, self._hvac_mode)

    @property
    def native_value(self) -> float | None:
        """Return the learned slope, or a live guess while unlearned.

        There is no fixed default for an unlearned profile: the MPC still has
        to evaluate this fan mode every cycle, so it substitutes a rank-scaled
        estimate from whichever fan is currently running (see
        ``MPCController.get_live_mode_slope``). Showing that instead of an
        empty field answers "what is the controller actually assuming right
        now" and gives the user a real number to start correcting from, rather
        than requiring them to guess a starting point themselves. Which case
        this is stays visible via the ``value_source`` attribute below.
        """
        learning = self._controller.learning
        if self._sample_count() >= MIN_MODE_PROFILE_SAMPLES:
            value = learning.get_mode_effective_slope(self._fan_mode, self._hvac_mode)
        else:
            live = self._controller.get_live_mode_slope(self._fan_mode, self._hvac_mode)
            value = live[0] if live is not None else None
        return round(value, 3) if value is not None else None

    async def async_set_native_value(self, value: float) -> None:
        """Persist a manually chosen effective slope for this profile.

        Replaces this profile's samples with synthetic ones producing exactly
        `value` (the same call the ``set_effective_slope`` service makes), so
        the profile becomes immediately "ready" -- real samples collected from
        here on blend in and gradually refine it, they don't reset it. The MPC
        still treats the profile as *unmeasured* until MIN_MODE_PROFILE_SAMPLES
        real samples exist (see ``value_source``): a seeded value is what the
        user believes, and the exploration guards exist to check it.
        """
        self._controller.learning.set_mode_effective_slope(self._fan_mode, self._hvac_mode, value)
        if self._on_change is not None:
            await self._on_change()
        # Guarded like the manager's own per-cycle push: an entity built for a
        # test, or before Home Assistant has finished registering it, has no
        # hass/platform yet and async_write_ha_state() would raise.
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        """Expose sampling and model details for this profile."""
        learning = self._controller.learning
        samples = self._sample_count()
        real_samples = learning.get_mode_real_sample_count(self._fan_mode, self._hvac_mode)
        ready = samples >= MIN_MODE_PROFILE_SAMPLES
        if not ready:
            value_source = "live_fallback_estimate"
        elif real_samples >= MIN_MODE_PROFILE_SAMPLES:
            value_source = "learned"
        elif real_samples > 0:
            value_source = "seeded_blended"
        else:
            value_source = "seeded"
        spread = learning.get_profile_spread(self._fan_mode, self._hvac_mode)
        if spread is None:
            quality = "unknown"
        elif spread < 0.15:
            quality = "good"
        elif spread < 0.30:
            quality = "fair"
        else:
            quality = "poor"
        # Gap-dependent slope model: effective_slope(error) = intercept + gain·error.
        # The displayed value is this model evaluated at REFERENCE_SLOPE_ERROR.
        model = learning.get_mode_slope_model(self._fan_mode, self._hvac_mode)
        if model is None:
            slope_intercept = None
            slope_gain = None
        else:
            slope_intercept = round(model[0], 3)
            slope_gain = round(model[1], 3)
        r_squared = learning.get_mode_slope_r2(self._fan_mode, self._hvac_mode)
        time_constant = learning.get_mode_time_constant(self._fan_mode, self._hvac_mode)
        return {
            "hvac_mode": self._hvac_mode,
            "fan_mode": self._fan_mode,
            "samples": samples,
            # Measured samples only: the count that moves the value and that the
            # MPC's exploration guards read. ``samples`` also includes synthetic
            # ones written by set_effective_slope.
            "real_samples": real_samples,
            "min_samples_required": MIN_MODE_PROFILE_SAMPLES,
            "ready": ready,
            # Tells apart a real measurement ("learned"), a user-seeded value
            # ("seeded"), a seeded value already pulled by a few measurements
            # ("seeded_blended") and the live guess shown while nothing is known
            # -- the state alone can't, since all are plain numbers.
            "value_source": value_source,
            "spread": spread,
            "quality": quality,
            "slope_intercept": slope_intercept,
            "slope_gain": slope_gain,
            "reference_error": REFERENCE_SLOPE_ERROR,
            "model_r_squared": round(r_squared, 3) if r_squared is not None else None,
            "thermal_time_constant_h": round(time_constant, 2) if time_constant is not None else None,
        }
