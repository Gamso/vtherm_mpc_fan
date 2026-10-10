"""services.yaml is UI metadata only -- Home Assistant does not validate a
service call against it, only against the voluptuous schema passed to
``async_register``. The two can silently drift apart: a field renamed in one
and not the other breaks every call made through the Developer Tools UI (which
is built from services.yaml) with an "extra keys not allowed" error, while the
YAML-only or script-based caller who already knew the real field name never
notices. This happened once already -- services.yaml kept `climate_entity`
after the plugin migration renamed the schema field to `target_vtherm`.
"""

from pathlib import Path

import yaml

from custom_components.vtherm_mpc_fan import (
    SERVICE_APPLY_LEARNED_SETTINGS,
    SERVICE_FORCE_FAN,
    SERVICE_RESET_LEARNING,
    SERVICE_SET_EFFECTIVE_SLOPE,
    ATTR_TARGET_VTHERM,
)

SERVICES_YAML = Path(__file__).parent.parent / "custom_components" / "vtherm_mpc_fan" / "services.yaml"

EXPECTED_FIELDS = {
    SERVICE_APPLY_LEARNED_SETTINGS: {ATTR_TARGET_VTHERM},
    SERVICE_RESET_LEARNING: {ATTR_TARGET_VTHERM},
    SERVICE_SET_EFFECTIVE_SLOPE: {
        ATTR_TARGET_VTHERM,
        "hvac_mode",
        "fan_mode",
        "effective_slope",
    },
    SERVICE_FORCE_FAN: {ATTR_TARGET_VTHERM, "fan_mode", "duration_minutes"},
}


def test_services_yaml_is_valid_yaml_and_declares_every_service() -> None:
    """The UI metadata file must at least parse and cover every registered service."""
    spec = yaml.safe_load(SERVICES_YAML.read_text(encoding="utf-8"))
    assert set(spec) == set(EXPECTED_FIELDS)


def test_services_yaml_field_names_match_the_voluptuous_schema() -> None:
    """Every field key in services.yaml must be a key the schema actually accepts.

    This is the check that would have caught the climate_entity/target_vtherm
    drift: services.yaml's fields dict and __init__.py's schema keys must name
    the same thing, or the Developer Tools form sends a key the schema rejects.
    """
    spec = yaml.safe_load(SERVICES_YAML.read_text(encoding="utf-8"))
    for service, expected_fields in EXPECTED_FIELDS.items():
        declared_fields = set(spec[service].get("fields", {}))
        assert declared_fields == expected_fields, f"{service}: services.yaml declares {declared_fields}, but the schema expects {expected_fields}"
