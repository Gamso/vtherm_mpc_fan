"""Config and options flows driven through a real Home Assistant core.

test_config_flow.py covers the pure helpers; these tests instantiate the flows
the way the UI does, which is where a field missing from the form (B-2) or an
error key the translations do not carry (B-1) actually shows.
"""

import json
from pathlib import Path

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vtherm_mpc_fan.const import (
    CONF_DEADBAND,
    CONF_FAN_MODE_ORDER,
    CONF_FIXED_FAN_HVAC_MODES,
    CONF_FIXED_FAN_SPEED,
    CONF_MIN_INTERVAL,
    CONF_TARGET_VTHERM,
    DOMAIN,
    VTHERM_DOMAIN,
)

VTHERM_UID = "vtherm-a"
FAN_MODES = ["low", "med", "high"]
TRANSLATIONS = json.loads((Path(__file__).parent.parent / "custom_components" / DOMAIN / "translations" / "en.json").read_text(encoding="utf-8"))


def _register_vtherm(hass, state: str = "cool", attributes: dict | None = None, platform: str = VTHERM_DOMAIN) -> str:
    """Register a climate entity for *platform* and give it a state; return its entity_id."""
    entity_id = er.async_get(hass).async_get_or_create("climate", platform, VTHERM_UID, suggested_object_id="living_room").entity_id
    hass.states.async_set(entity_id, state, attributes if attributes is not None else {})
    return entity_id


def _entry(hass, **options) -> MockConfigEntry:
    """Add a plugin entry for the registered VTherm, with *options* stored."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=f"{DOMAIN}-{VTHERM_UID}", data={CONF_TARGET_VTHERM: VTHERM_UID}, options=options)
    entry.add_to_hass(hass)
    return entry


def _ranks(order: list[str]) -> dict[str, str]:
    """Return the per-rank dropdown values for *order*."""
    return {f"{CONF_FAN_MODE_ORDER}_{rank}": mode for rank, mode in enumerate(order)}


def _fields(result) -> set[str]:
    """Return the field names of the form a flow step shows."""
    return {str(key) for key in result["data_schema"].schema}


def _assert_translated(result, section: str) -> None:
    """Every error the form shows must resolve under ``<section>.error``."""
    for key in result["errors"].values():
        assert key in TRANSLATIONS[section]["error"], f"{section}.error.{key} is not translated"


# --- Config flow (user step) ------------------------------------------------


async def test_user_step_creates_an_entry_keyed_on_the_vtherm_unique_id(integration) -> None:
    """The entry stores the VTherm unique_id, not its renameable entity_id."""
    hass = integration
    entity_id = _register_vtherm(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TARGET_VTHERM: entity_id, CONF_DEADBAND: 0.3})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TARGET_VTHERM] == VTHERM_UID
    assert result["result"].unique_id == f"{DOMAIN}-{VTHERM_UID}"


@pytest.mark.parametrize(
    ("platform", "expected"),
    [(None, "invalid_entity"), ("generic_thermostat", "not_a_vtherm")],
)
async def test_user_step_rejects_what_is_not_a_vtherm(integration, platform, expected) -> None:
    """An unregistered entity, or a climate of another integration, is refused with a translated error."""
    hass = integration
    entity_id = "climate.ghost" if platform is None else _register_vtherm(hass, platform=platform)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TARGET_VTHERM: entity_id})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_TARGET_VTHERM: expected}
    _assert_translated(result, "config")


# --- Options flow -----------------------------------------------------------


async def test_options_flow_offers_ladder_and_fixed_speed_for_a_reporting_vtherm(integration) -> None:
    """A VTherm that reports its modes gets the full form."""
    hass = integration
    _register_vtherm(hass, attributes={"fan_modes": ["auto", *FAN_MODES], "hvac_modes": ["off", "heat", "cool", "dry"]})
    entry = _entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    fields = _fields(result)
    assert {CONF_FIXED_FAN_HVAC_MODES, CONF_FIXED_FAN_SPEED} <= fields
    assert set(_ranks(FAN_MODES)) <= fields


async def test_options_flow_duplicate_rank_shows_a_translated_base_error(integration) -> None:
    """The same speed at two ranks is refused with fan_order_invalid, translated under options.error."""
    hass = integration
    _register_vtherm(hass, attributes={"fan_modes": FAN_MODES, "hvac_modes": ["heat", "cool", "dry"]})
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    result = await hass.config_entries.options.async_configure(result["flow_id"], _ranks(["low", "low", "high"]))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "fan_order_invalid"}
    _assert_translated(result, "options")


async def test_options_flow_fixed_modes_without_speed_show_a_translated_field_error(integration) -> None:
    """Pinning a mode without a speed is refused on the speed field, which the form shows."""
    hass = integration
    _register_vtherm(hass, attributes={"fan_modes": FAN_MODES, "hvac_modes": ["heat", "cool", "dry"]})
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    result = await hass.config_entries.options.async_configure(result["flow_id"], {**_ranks(FAN_MODES), CONF_FIXED_FAN_HVAC_MODES: ["dry"]})

    assert result["errors"] == {CONF_FIXED_FAN_SPEED: "fixed_fan_speed_required"}
    assert CONF_FIXED_FAN_SPEED in _fields(result)
    _assert_translated(result, "options")


async def test_options_flow_saves_the_order_and_the_pin(integration) -> None:
    """A valid submission stores the assembled order and the fixed-speed choice."""
    hass = integration
    _register_vtherm(hass, attributes={"fan_modes": FAN_MODES, "hvac_modes": ["heat", "cool", "dry"]})
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**_ranks(["med", "low", "high"]), CONF_FIXED_FAN_HVAC_MODES: ["dry"], CONF_FIXED_FAN_SPEED: "high"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_FAN_MODE_ORDER] == ["med", "low", "high"]
    assert entry.options[CONF_FIXED_FAN_HVAC_MODES] == ["dry"]
    assert entry.options[CONF_FIXED_FAN_SPEED] == "high"


async def test_options_flow_with_the_vtherm_unavailable_is_not_a_dead_end(integration) -> None:
    """B-2: no speed is known, so the fixed-speed fields are not offered and the form saves.

    The modes used to be offered from the fallback list while the speed field
    was missing: ticking dry errored on a field the form did not show.
    """
    hass = integration
    _register_vtherm(hass, state="unavailable")
    entry = _entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert not {CONF_FIXED_FAN_HVAC_MODES, CONF_FIXED_FAN_SPEED} & _fields(result)

    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_MIN_INTERVAL: 15})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MIN_INTERVAL] == 15


async def test_options_flow_with_the_vtherm_unavailable_keeps_a_stored_pin(integration) -> None:
    """A saved pin stays editable while the VTherm is down, and survives a save."""
    hass = integration
    _register_vtherm(hass, state="unavailable")
    entry = _entry(hass, **{CONF_FIXED_FAN_HVAC_MODES: ["dry"], CONF_FIXED_FAN_SPEED: "high"})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert {CONF_FIXED_FAN_HVAC_MODES, CONF_FIXED_FAN_SPEED} <= _fields(result)

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_FIXED_FAN_HVAC_MODES] == ["dry"]
    assert entry.options[CONF_FIXED_FAN_SPEED] == "high"
