"""The plugin logs through vtherm_api so its lines reach VTherm's log export."""

import logging

from vtherm_api import VThermLogger

from custom_components.vtherm_mpc_fan import log as log_module
from custom_components.vtherm_mpc_fan.manager import _LOGGER as MANAGER_LOGGER


def test_plugin_loggers_are_vtherm_loggers() -> None:
    """VTherm's collector only receives records emitted through a VThermLogger."""
    assert isinstance(MANAGER_LOGGER, VThermLogger)
    assert isinstance(log_module.get_logger("custom_components.vtherm_mpc_fan.test"), VThermLogger)


def test_event_lines_use_the_core_format(caplog) -> None:
    """write_event_log produces the same "<subject> - ... NEW EVENT: ..." line as the core."""
    logger = log_module.get_logger("custom_components.vtherm_mpc_fan.test_event")
    with caplog.at_level(logging.INFO, logger="custom_components.vtherm_mpc_fan"):
        log_module.write_event_log(logger, "MpcFanManager-Living room", "fan mode low -> high")

    assert "MpcFanManager-Living room - ---------------------> NEW EVENT: fan mode low -> high" in caplog.text


def test_plain_logging_is_the_fallback_without_vtherm_api(monkeypatch, caplog) -> None:
    """Without vtherm_api the plugin still logs, with the same event line."""
    monkeypatch.setattr(log_module, "_get_vtherm_logger", None)
    monkeypatch.setattr(log_module, "_write_event_log", None)

    logger = log_module.get_logger("custom_components.vtherm_mpc_fan.fallback")
    assert type(logger) is logging.Logger  # pylint: disable=unidiomatic-typecheck
    with caplog.at_level(logging.INFO, logger="custom_components.vtherm_mpc_fan"):
        log_module.write_event_log(logger, "MpcFanManager-Bedroom", "force_fan override cancelled")

    assert "MpcFanManager-Bedroom - ---------------------> NEW EVENT: force_fan override cancelled" in caplog.text
