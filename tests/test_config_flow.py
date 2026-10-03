"""Tests for VTherm MPC Fan config-flow guards.

Duplicate-target protection is no longer tested here: as a VTherm plugin the
flow claims the unique_id ``vtherm_mpc_fan-<vtherm uid>`` and lets Home
Assistant's own ``_abort_if_unique_id_configured`` reject a second entry for the
same thermostat, so there is no hand-rolled guard left to cover.
"""

from unittest.mock import MagicMock

from custom_components.vtherm_mpc_fan.config_flow import (
    _fan_order_field_key,
    _fan_order_schema,
    _fixed_fan_schema,
    assemble_fan_order,
    extract_all_fan_modes,
    extract_fan_modes,
    extract_fixed_fan_hvac_modes,
    validate_fan_order,
    validate_fixed_fan,
)
from custom_components.vtherm_mpc_fan.const import (
    CONF_FAN_MODE_ORDER,
    CONF_FIXED_FAN_HVAC_MODES,
    CONF_FIXED_FAN_SPEED,
)
from custom_components.vtherm_mpc_fan.manager import apply_configured_fan_order
from custom_components.vtherm_mpc_fan.registry import find_conflicting_plugin


def test_extract_fan_modes_filters_auto_and_off() -> None:
    """Only manual speeds can be ordered; auto/off are not strength levels."""
    state = MagicMock()
    state.attributes = {"fan_modes": ["Auto", "silent", "low", "med", "high", "superhigh", "off"]}

    assert extract_fan_modes(state) == ["silent", "low", "med", "high", "superhigh"]


def test_extract_fan_modes_handles_missing_state() -> None:
    """A climate entity with no state yet yields no options, not a crash."""
    assert extract_fan_modes(None) == []

    state = MagicMock()
    state.attributes = {}
    assert extract_fan_modes(state) == []


def test_validate_fan_order_accepts_nothing_to_validate_and_a_permutation() -> None:
    """None means 'no fan modes detected yet'; a full permutation is valid."""
    detected = ["silent", "low", "med"]
    assert validate_fan_order(None, detected) is None
    assert validate_fan_order(["med", "silent", "low"], detected) is None


def test_validate_fan_order_rejects_a_duplicate_pick() -> None:
    """Picking the same speed at two ranks is the only way this widget can go wrong."""
    detected = ["silent", "low", "med"]
    assert validate_fan_order(["silent", "silent", "med"], detected) == "fan_order_invalid"


def test_assemble_fan_order_reads_each_rank_in_order() -> None:
    """The per-rank dropdown values are collected back into one ordered list."""
    detected = ["silent", "low", "med"]
    user_input = {
        _fan_order_field_key(0): "med",
        _fan_order_field_key(1): "silent",
        _fan_order_field_key(2): "low",
    }
    assert assemble_fan_order(user_input, detected) == ["med", "silent", "low"]


def test_assemble_fan_order_is_none_when_nothing_is_detected() -> None:
    """No fan modes known yet means there is nothing to assemble."""
    assert assemble_fan_order({}, []) is None


def test_assemble_fan_order_is_none_on_a_partial_submission() -> None:
    """A form that hasn't been submitted (or is missing a rank) yields None, not a crash."""
    detected = ["silent", "low", "med"]
    assert assemble_fan_order({_fan_order_field_key(0): "silent"}, detected) is None


def test_fan_order_schema_has_one_dropdown_per_detected_speed() -> None:
    """Each rank is its own field, constrained to the detected options."""
    detected = ["silent", "low", "med"]
    schema = _fan_order_schema({}, detected)

    keys = {str(key) for key in schema}
    assert keys == {_fan_order_field_key(i) for i in range(3)}


def test_fan_order_schema_is_empty_when_nothing_is_detected() -> None:
    """No fan modes known yet means no dropdowns to show."""
    assert _fan_order_schema({}, []) == {}


def test_fan_order_schema_defaults_to_the_current_order() -> None:
    """Reopening the form must show the order actually in effect, not the detected one."""
    detected = ["silent", "low", "med"]
    current_order = ["low", "med", "silent"]

    schema = _fan_order_schema({CONF_FAN_MODE_ORDER: current_order}, detected)

    defaults = [key.default() for key in schema]
    assert defaults == current_order


def test_fan_order_schema_falls_back_on_a_stale_saved_order() -> None:
    """A saved order naming a speed the underlying no longer has resets to detected."""
    detected = ["silent", "low", "med"]
    stale = ["silent", "low", "extinct"]

    schema = _fan_order_schema({CONF_FAN_MODE_ORDER: stale}, detected)

    defaults = [key.default() for key in schema]
    assert defaults == detected


def test_fan_order_schema_preserves_a_duplicate_resubmission() -> None:
    """An erroring resubmission (duplicate pick) is shown back as-is, not silently reset.

    Every element of a duplicate submission is still a valid detected mode, which
    must be told apart from genuine staleness (a mode the underlying no longer has).
    """
    detected = ["silent", "low", "med"]
    duplicated = ["silent", "silent", "med"]

    schema = _fan_order_schema({CONF_FAN_MODE_ORDER: duplicated}, detected)

    defaults = [key.default() for key in schema]
    assert defaults == duplicated


def test_apply_configured_fan_order_reorders() -> None:
    """The configured order wins over the order the underlying reports."""
    detected = ["high", "low", "superhigh", "med"]  # reported jumbled
    configured = ["low", "med", "high", "superhigh"]

    assert apply_configured_fan_order(detected, configured) == configured


def test_apply_configured_fan_order_without_config_is_passthrough() -> None:
    """No configured order means the detected order is used unchanged."""
    detected = ["low", "med", "high"]

    assert apply_configured_fan_order(detected, None) == detected
    assert apply_configured_fan_order(detected, []) == detected


def _hass_with_entries(entries_by_domain: dict) -> MagicMock:
    """A hass whose config-entry registry returns the given entries per domain."""
    hass = MagicMock()
    hass.config_entries.async_entries = MagicMock(side_effect=lambda domain: entries_by_domain.get(domain, []))
    return hass


def test_conflict_guard_detects_another_fan_plugin_on_the_same_vtherm() -> None:
    """Two controllers on one fan make the speed flap; the second is refused."""
    entry = MagicMock()
    entry.data = {"target_vtherm_unique_id": "vtherm-uid"}
    hass = _hass_with_entries({"vtherm_auto_fan_extended": [entry]})

    assert find_conflicting_plugin(hass, "vtherm-uid") == "vtherm_auto_fan_extended"


def test_conflict_guard_keys_on_the_target_not_on_installation() -> None:
    """The other plugin driving a different thermostat is not a conflict."""
    entry = MagicMock()
    entry.data = {"target_vtherm_unique_id": "some-other-vtherm"}
    hass = _hass_with_entries({"vtherm_auto_fan_extended": [entry]})

    assert find_conflicting_plugin(hass, "vtherm-uid") is None


def test_conflict_guard_is_quiet_when_no_other_plugin_is_installed() -> None:
    """A registry with nothing in it must not look like a conflict."""
    assert find_conflicting_plugin(_hass_with_entries({}), "vtherm-uid") is None


def test_apply_configured_fan_order_tolerates_drift() -> None:
    """A speed added by the underlying later is appended; a removed one is dropped."""
    detected = ["low", "med", "high", "turbo"]  # 'turbo' appeared after configuration
    configured = ["low", "med", "high", "retired"]  # 'retired' no longer exists

    assert apply_configured_fan_order(detected, configured) == ["low", "med", "high", "turbo"]


def test_extract_fixed_fan_hvac_modes_drops_off_and_regulated_modes() -> None:
    """Pinnable modes exclude off and the always-regulated heat/cool."""
    state = MagicMock()
    state.attributes = {"hvac_modes": ["off", "heat", "cool", "dry", "fan_only"]}
    assert extract_fixed_fan_hvac_modes(state) == ["dry", "fan_only"]


def test_extract_fixed_fan_hvac_modes_falls_back_when_nothing_is_reported() -> None:
    """An unavailable climate still leaves the usual modes selectable."""
    assert extract_fixed_fan_hvac_modes(None) == ["dry", "fan_only"]
    state = MagicMock()
    state.attributes = {}
    assert extract_fixed_fan_hvac_modes(state) == ["dry", "fan_only"]


def test_extract_fixed_fan_hvac_modes_offers_nothing_a_climate_cannot_enter() -> None:
    """A climate that reports only off/heat/cool gets no pinnable mode.

    The fallback is for a climate that reports nothing; one that does report
    its modes must not be offered dry/fan_only it cannot take.
    """
    state = MagicMock()
    state.attributes = {"hvac_modes": ["off", "heat", "cool"]}
    assert extract_fixed_fan_hvac_modes(state) == []


def _schema_keys(schema: dict) -> set[str]:
    """Return the field names of a voluptuous schema dict."""
    return {str(key) for key in schema}


def test_fixed_fan_fields_are_hidden_when_no_speed_is_selectable() -> None:
    """Offering the modes without any speed would be a dead end.

    Ticking a mode requires a speed; with the climate unavailable no speed is
    known, so the form would reject every submission on a field it does not
    show. Both fields are dropped together instead.
    """
    assert _fixed_fan_schema({}, ["dry", "fan_only"], []) == {}


def test_fixed_fan_fields_are_hidden_when_no_mode_can_be_pinned() -> None:
    """A climate with only heat/cool has nothing to pin."""
    assert _fixed_fan_schema({}, [], ["low", "high"]) == {}


def test_fixed_fan_fields_come_as_a_pair() -> None:
    """With modes and speeds available, both fields are offered."""
    keys = _schema_keys(_fixed_fan_schema({}, ["dry"], ["low", "high"]))
    assert keys == {CONF_FIXED_FAN_HVAC_MODES, CONF_FIXED_FAN_SPEED}


def test_stored_fixed_fan_choices_keep_the_fields_while_the_climate_is_unavailable() -> None:
    """A saved pin stays editable, and is not silently dropped, while nothing is reported."""
    stored = {CONF_FIXED_FAN_HVAC_MODES: ["dry"], CONF_FIXED_FAN_SPEED: "superhigh"}
    keys = _schema_keys(_fixed_fan_schema(stored, [], []))
    assert keys == {CONF_FIXED_FAN_HVAC_MODES, CONF_FIXED_FAN_SPEED}


def test_extract_all_fan_modes_keeps_auto() -> None:
    """A pinned speed may be auto, unlike the MPC's manual-only ladder."""
    state = MagicMock()
    state.attributes = {"fan_modes": ["auto", "low", "superhigh"]}
    assert extract_all_fan_modes(state) == ["auto", "low", "superhigh"]
    assert extract_all_fan_modes(None) == []


def test_validate_fixed_fan_accepts_valid_choices() -> None:
    """A speed with modes, or no pinned modes at all, is valid."""
    assert validate_fixed_fan(["dry", "fan_only"], "superhigh") == {}
    assert validate_fixed_fan([], None) == {}
    assert validate_fixed_fan(None, None) == {}


def test_validate_fixed_fan_requires_a_speed_for_fixed_modes() -> None:
    """Pinning modes without a speed would silently do nothing."""
    assert validate_fixed_fan(["dry"], None) == {CONF_FIXED_FAN_SPEED: "fixed_fan_speed_required"}
