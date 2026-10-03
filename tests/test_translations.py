"""Every error key a flow can emit must be translated where Home Assistant looks.

Home Assistant resolves a flow error ``errors[field] = "key"`` against
``<config|options>.error.key`` -- never against ``step.<id>.error``, and never
by the field name. Both mistakes render the raw key in the UI and neither is
caught at runtime, which is how the options-flow errors once ended up under
``options.step.init.error`` with the value ``fan_order_invalid`` stored as
``"base"``. The keys are collected from the flow source itself, so a new error
added to a flow without its translation fails here.
"""

import ast
import json
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parent.parent / "custom_components" / "vtherm_mpc_fan"
LANGUAGES = ("en", "fr")
FLOW_SECTIONS = {
    "VThermMpcFanConfigFlow": "config",
    "VThermMpcFanOptionsFlow": "options",
}


def _load_translations(language: str) -> dict:
    """Return one translation file, parsed."""
    return json.loads((COMPONENT / "translations" / f"{language}.json").read_text(encoding="utf-8"))


def _returned_error_keys(func: ast.FunctionDef) -> set[str]:
    """Return the string literals a validator hands back as error keys.

    Covers both shapes used in config_flow.py: ``return "key"`` and
    ``return {FIELD: "key"}``.
    """
    keys: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            keys.add(value.value)
        elif isinstance(value, ast.Dict):
            keys.update(v.value for v in value.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return keys


def _assigned_error_keys(node: ast.AST) -> set[str]:
    """Return the string literals assigned as ``errors[...] = "key"`` under *node*."""
    keys: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Assign) or not isinstance(child.value, ast.Constant):
            continue
        for target in child.targets:
            if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and target.value.id == "errors":
                keys.add(child.value.value)
    return keys


def _emitted_error_keys() -> dict[str, set[str]]:
    """Map each translation section to the error keys its flow can emit.

    A flow emits what it assigns into ``errors`` directly, plus what the
    module-level ``validate_*`` helpers it calls return.
    """
    tree = ast.parse((COMPONENT / "config_flow.py").read_text(encoding="utf-8"))
    validators = {node.name: _returned_error_keys(node) for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("validate_")}

    emitted: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in FLOW_SECTIONS:
            continue
        keys = _assigned_error_keys(node)
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in validators:
                keys |= validators[call.func.id]
        emitted[FLOW_SECTIONS[node.name]] = keys
    return emitted


def test_the_scan_finds_the_errors_it_is_meant_to_guard() -> None:
    """Guard the guard: an AST walk that silently found nothing would pass vacuously."""
    emitted = _emitted_error_keys()
    assert {"invalid_entity", "not_a_vtherm", "fan_already_driven"} <= emitted["config"]
    assert {"fan_order_invalid", "fixed_fan_speed_required"} <= emitted["options"]


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_emitted_error_is_translated_under_the_flow_error_section(language: str) -> None:
    """Each key a flow can emit exists under ``<section>.error`` in every language."""
    translations = _load_translations(language)
    for section, keys in _emitted_error_keys().items():
        translated = set(translations[section].get("error", {}))
        missing = keys - translated
        assert not missing, f"{language}.json: {section}.error lacks {sorted(missing)}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_no_error_block_is_nested_under_a_step(language: str) -> None:
    """``step.<id>.error`` is never read by Home Assistant: an error placed there is lost."""
    translations = _load_translations(language)
    for section in FLOW_SECTIONS.values():
        for step_id, step in translations[section].get("step", {}).items():
            assert "error" not in step, f"{language}.json: {section}.step.{step_id}.error is never read"


def test_languages_translate_the_same_keys() -> None:
    """en and fr must not drift apart: a key missing in one renders raw in that language."""

    def _paths(tree: dict, prefix: str = "") -> set[str]:
        """Flatten a translation tree into its dotted key paths."""
        paths: set[str] = set()
        for key, value in tree.items():
            path = f"{prefix}.{key}" if prefix else key
            paths |= _paths(value, path) if isinstance(value, dict) else {path}
        return paths

    reference, *others = (_paths(_load_translations(language)) for language in LANGUAGES)
    for other in others:
        assert other == reference
