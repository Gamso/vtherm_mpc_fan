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
    assemble_fan_order,
    extract_fan_modes,
    validate_fan_order,
)
from custom_components.vtherm_mpc_fan.const import CONF_FAN_MODE_ORDER
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
    hass.config_entries.async_entries = MagicMock(
        side_effect=lambda domain: entries_by_domain.get(domain, [])
    )
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
