"""Logging helpers: route this plugin's records into Versatile Thermostat's log export.

VTherm's log collector (``VThermLogHandler``, behind its downloadable log
export) only receives records emitted through a ``VThermLogger``, and filters
them per thermostat on a ``"<thermostat> - "`` prefix. ``vtherm_api`` provides
both the logger factory and the "NEW EVENT" line the core writes on every
significant change. It ships with Versatile Thermostat rather than with this
plugin (see ``__init__._register_factory``), so the plain ``logging`` module is
the fallback when it is missing -- the plugin must still load and report it.
"""

from __future__ import annotations

import logging
from typing import Any

try:
    from vtherm_api import get_vtherm_logger as _get_vtherm_logger
    from vtherm_api import write_event_log as _write_event_log
except ImportError:  # pragma: no cover - exercised by test_log.py through the fallbacks below
    _get_vtherm_logger = None  # pylint: disable=invalid-name
    _write_event_log = None  # pylint: disable=invalid-name


def get_logger(name: str) -> logging.Logger:
    """Return the logger for *name*: a VThermLogger when vtherm_api is available."""
    if _get_vtherm_logger is None:
        return logging.getLogger(name)
    return _get_vtherm_logger(name)


def write_event_log(logger: logging.Logger, subject: Any, message: str) -> None:
    """Write a highlighted "NEW EVENT" line for *subject*, as the VTherm core does.

    *subject* is formatted with ``%s`` as the line's ``"<subject> - "`` prefix;
    the feature manager passes itself (``MpcFanManager-<thermostat name>``).
    """
    if _write_event_log is not None:
        _write_event_log(logger, subject, message)
        return
    logger.info("%s - ---------------------> NEW EVENT: %s --------------------------------------------------------", subject, message)
