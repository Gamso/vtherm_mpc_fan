"""Shared fixtures for the tests that run against a real Home Assistant core.

Most of the suite drives the manager and the MPC with lightweight stand-ins and
needs no event loop at all. The integration-level tests (setup/unload,
services, flows) use the ``hass`` fixture of pytest-homeassistant-custom-component
through ``integration`` below, which is deliberately *not* autouse: enabling
custom integrations requires ``hass``, and building a core for every pure test
would only slow the suite down.
"""

import pytest
from vtherm_api.vtherm_api import VThermAPI


@pytest.fixture
def integration(hass, enable_custom_integrations):  # pylint: disable=unused-argument
    """Yield a hass with custom integrations enabled and a clean VThermAPI.

    VThermAPI keeps the hass it was first given in a class attribute, so it is
    reset after each test: otherwise the next test's factory registration
    would land on the previous, already-stopped instance.
    """
    yield hass
    VThermAPI.reset_vtherm_api()
