"""Config and options flow for VTherm MPC Fan."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
    CONF_DATA_COLLECTION,
    CONF_DEADBAND,
    CONF_DEFROST_ENTITY,
    CONF_FAN_MODE_ORDER,
    CONF_FIXED_FAN_HVAC_MODES,
    CONF_FIXED_FAN_SPEED,
    CONF_MIN_INTERVAL,
    CONF_TARGET_VTHERM,
    DEFAULT_DATA_COLLECTION,
    DEFAULT_DEADBAND,
    DEFAULT_MIN_INTERVAL,
    DOMAIN,
    PROFILE_HVAC_MODES,
    VTHERM_DOMAIN,
)
from .registry import find_conflicting_plugin

FALLBACK_FIXED_FAN_HVAC_MODES = ("dry", "fan_only")


def extract_fan_modes(state) -> list[str]:
    """Return the manual fan modes a climate state exposes (excludes auto/off)."""
    if state is None:
        return []
    raw_modes = state.attributes.get("fan_modes")
    if not raw_modes:
        return []
    return [
        mode
        for mode in raw_modes
        if isinstance(mode, str) and mode.lower() not in {"auto", "off"}
    ]


def extract_fixed_fan_hvac_modes(state) -> list[str]:
    """Return the HVAC modes a climate state offers for a fixed fan speed.

    Excludes off, and heat/cool, which the MPC always regulates. Falls back to
    the usual non-regulated modes only when the entity reports no
    ``hvac_modes`` at all (unavailable, or not published yet), so the option
    stays usable meanwhile. A climate that does report its modes and offers
    nothing beyond off/heat/cool gets an empty list: proposing dry or fan_only
    there would offer modes the thermostat can never enter.
    """
    raw_modes = state.attributes.get("hvac_modes") if state is not None else None
    if not raw_modes:
        return list(FALLBACK_FIXED_FAN_HVAC_MODES)
    return [
        mode
        for mode in raw_modes
        if isinstance(mode, str) and mode.lower() != "off" and mode not in PROFILE_HVAC_MODES
    ]


def extract_all_fan_modes(state) -> list[str]:
    """Return every fan mode a climate state exposes, auto included.

    A pinned speed has no need of the MPC's manual-only filter: "auto" is a
    legitimate speed to hold in dry or fan-only mode.
    """
    raw_modes = state.attributes.get("fan_modes") if state is not None else None
    return [mode for mode in raw_modes or [] if isinstance(mode, str)]


def validate_fixed_fan(fixed: list[str] | None, fixed_speed: str | None) -> dict[str, str]:
    """Return per-field error keys for the fixed-speed HVAC mode choice.

    Pinning modes without saying which speed would do nothing, so that is
    rejected rather than silently ignored.
    """
    if fixed and not fixed_speed:
        return {CONF_FIXED_FAN_SPEED: "fixed_fan_speed_required"}
    return {}


def _fixed_fan_schema(
    defaults: dict[str, Any], mode_choices: list[str], speed_choices: list[str]
) -> dict:
    """Build the fixed-speed fields: which HVAC modes pin the fan, and to what.

    Already-stored choices stay selectable even if the climate stopped
    reporting them, so a saved value is never defaulted outside its own options.

    The two fields are offered together or not at all. Selecting a mode is
    rejected unless a speed is chosen too (``validate_fixed_fan``), so showing
    the modes while no speed is selectable -- a climate that is unavailable and
    has no stored speed -- would be a dead end: every submission with a mode
    ticked errors on a field the form does not even display.
    """
    stored_modes = defaults.get(CONF_FIXED_FAN_HVAC_MODES) or []
    mode_choices = mode_choices + [m for m in stored_modes if m not in mode_choices]
    stored_speed = defaults.get(CONF_FIXED_FAN_SPEED)
    if stored_speed and stored_speed not in speed_choices:
        speed_choices = speed_choices + [stored_speed]
    if not mode_choices or not speed_choices:
        return {}

    schema: dict[Any, Any] = {
        vol.Optional(
            CONF_FIXED_FAN_HVAC_MODES, default=stored_modes
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=mode_choices,
                multiple=True,
                mode=selector.SelectSelectorMode.LIST,
            )
        ),
        vol.Optional(
            CONF_FIXED_FAN_SPEED, default=defaults.get(CONF_FIXED_FAN_SPEED, vol.UNDEFINED)
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=speed_choices, mode=selector.SelectSelectorMode.DROPDOWN
            )
        ),
    }
    return schema


def _fan_order_field_key(rank: int) -> str:
    """Return the schema key for the dropdown at the given rank (0 = weakest)."""
    return f"{CONF_FAN_MODE_ORDER}_{rank}"


def validate_fan_order(assembled: list[str] | None, detected: list[str]) -> str | None:
    """Return an error key when the assembled per-rank order is not usable, else None.

    ``assembled`` is None when there is nothing to validate (no fan modes
    detected yet). Otherwise every detected speed must appear exactly once:
    picking the same speed at two ranks is the only way this can go wrong, since
    each rank is an independent dropdown rather than a shared pool.
    """
    if assembled is None:
        return None
    if sorted(assembled) != sorted(detected):
        return "fan_order_invalid"
    return None


def assemble_fan_order(user_input: dict[str, Any], detected: list[str]) -> list[str] | None:
    """Collect the per-rank dropdown values back into an ordered list.

    Returns None when there is nothing to assemble (no fan modes detected), so
    the caller can tell "not applicable" apart from a genuine (if invalid)
    submission.
    """
    if not detected:
        return None
    keys = [_fan_order_field_key(rank) for rank in range(len(detected))]
    if not all(key in user_input for key in keys):
        return None
    return [user_input[key] for key in keys]


def _fan_order_schema(defaults: dict[str, Any], detected: list[str]) -> dict:
    """Build one ordered dropdown per detected fan speed, weakest rank first.

    A multi-select cannot express order: Home Assistant's checkbox-list widget
    stores values in click order, but the checkboxes show no positional
    feedback, and pre-filled with every speed already checked (the natural
    default) there is no visible cue that order is even what the field means.
    Worse, unchecking and rechecking a single box silently moves it to the end
    of the stored order -- a config field able to invert the safety-critical fan
    ladder with no visible warning. One dropdown per rank makes the current
    order legible at a glance, and changing it an explicit, single-field edit.
    """
    if not detected:
        return {}

    current = defaults.get(CONF_FAN_MODE_ORDER) or detected
    if any(mode not in detected for mode in current):
        # Stale order from a climate entity whose fan modes changed since this
        # was last saved; fall back rather than default a dropdown to a value
        # not in its own options list. A duplicate-but-valid list (an erroring
        # resubmission being redisplayed) is deliberately left untouched, so the
        # user sees exactly what they picked rather than a silent reset.
        current = detected

    schema: dict[Any, Any] = {}
    for rank, mode in enumerate(detected):
        default = current[rank] if rank < len(current) else mode
        schema[vol.Required(_fan_order_field_key(rank), default=default)] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=detected, mode=selector.SelectSelectorMode.DROPDOWN
            )
        )
    return schema


def _settings_schema(defaults: dict[str, Any]) -> dict:
    """Build the fixed settings fields, shared by the config and options flows."""
    return {
        vol.Optional(
            CONF_DEADBAND, default=defaults.get(CONF_DEADBAND, DEFAULT_DEADBAND)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.0, max=5.0, step=0.1, unit_of_measurement="°C"
            )
        ),
        vol.Optional(
            CONF_MIN_INTERVAL,
            default=defaults.get(CONF_MIN_INTERVAL, DEFAULT_MIN_INTERVAL),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=60, step=1, unit_of_measurement="min"
            )
        ),
        vol.Optional(
            CONF_DATA_COLLECTION,
            default=defaults.get(CONF_DATA_COLLECTION, DEFAULT_DATA_COLLECTION),
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_DEFROST_ENTITY,
            default=defaults.get(CONF_DEFROST_ENTITY, vol.UNDEFINED),
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["binary_sensor", "sensor", "input_boolean"]
            )
        ),
    }


# `domain=` is consumed by ConfigFlow.__init_subclass__, which type checkers do
# not model; the ignore silences that on an otherwise standard HA idiom.
class VThermMpcFanConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Attach an MPC fan controller to a Versatile Thermostat."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Pick the target VTherm and set the controller options.

        The fan-mode ladder is not offered here: at this point the underlying may
        not have published its ``fan_modes`` yet. It is set from the options flow,
        which reads the modes the running manager actually sees.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            entity_id = user_input[CONF_TARGET_VTHERM]
            registry_entry = er.async_get(self.hass).async_get(entity_id)

            if registry_entry is None or registry_entry.unique_id is None:
                errors[CONF_TARGET_VTHERM] = "invalid_entity"
            elif registry_entry.platform != VTHERM_DOMAIN:
                errors[CONF_TARGET_VTHERM] = "not_a_vtherm"
            elif find_conflicting_plugin(self.hass, registry_entry.unique_id):
                # Caught here rather than only at runtime so the user finds out
                # while choosing, not from a log line after the fan starts flapping.
                errors[CONF_TARGET_VTHERM] = "fan_already_driven"

            if not errors:
                await self.async_set_unique_id(f"{DOMAIN}-{registry_entry.unique_id}")
                self._abort_if_unique_id_configured()

                state = self.hass.states.get(entity_id)
                data = {
                    key: value
                    for key, value in user_input.items()
                    if key != CONF_TARGET_VTHERM
                }
                data[CONF_TARGET_VTHERM] = registry_entry.unique_id
                return self.async_create_entry(
                    title=state.name if state is not None else entity_id, data=data
                )

        schema = {
            vol.Required(CONF_TARGET_VTHERM): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=CLIMATE_DOMAIN, integration=VTHERM_DOMAIN
                )
            ),
            **_settings_schema(user_input or {}),
        }

        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(schema), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "VThermMpcFanOptionsFlow":
        """Return the options flow handler.

        The entry is deliberately not passed on: Home Assistant sets
        ``OptionsFlow.config_entry`` itself, and assigning it here has been
        deprecated since 2024.11. The parameter stays because the signature is
        part of the contract HA calls this with.
        """
        del config_entry
        return VThermMpcFanOptionsFlow()


class VThermMpcFanOptionsFlow(OptionsFlow):
    """Edit the controller options of an existing entry (the target is fixed)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Show and persist the controller options."""
        current = {**self.config_entry.data, **self.config_entry.options}
        errors: dict[str, str] = {}

        state = self._target_state(current.get(CONF_TARGET_VTHERM))
        detected = extract_fan_modes(state)

        if user_input is not None:
            fan_order = assemble_fan_order(user_input, detected)
            order_error = validate_fan_order(fan_order, detected)
            if order_error:
                errors["base"] = order_error
            errors.update(
                validate_fixed_fan(
                    user_input.get(CONF_FIXED_FAN_HVAC_MODES),
                    user_input.get(CONF_FIXED_FAN_SPEED),
                )
            )
            if not errors:
                data = {
                    key: value
                    for key, value in user_input.items()
                    if not key.startswith(f"{CONF_FAN_MODE_ORDER}_")
                }
                if fan_order is not None:
                    data[CONF_FAN_MODE_ORDER] = fan_order
                return self.async_create_entry(title="", data=data)

            # Re-show exactly what was submitted (duplicates included) so the
            # mistake is visible in place, rather than resetting to the old order.
            current = {**current, CONF_FAN_MODE_ORDER: fan_order, **user_input}

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    **_settings_schema(current),
                    **_fixed_fan_schema(
                        current,
                        extract_fixed_fan_hvac_modes(state),
                        extract_all_fan_modes(state),
                    ),
                    **_fan_order_schema(current, detected),
                }
            ),
            errors=errors,
        )

    def _target_state(self, target_unique_id: str | None):
        """Return the state of the VTherm this entry targets, or None."""
        if not target_unique_id:
            return None
        entity_id = er.async_get(self.hass).async_get_entity_id(
            CLIMATE_DOMAIN, VTHERM_DOMAIN, target_unique_id
        )
        if entity_id is None:
            return None
        return self.hass.states.get(entity_id)
