"""Dev-only: create the VTherm and MPC-fan config entries so the bench is turnkey.

Both Versatile Thermostat and this plugin are config-entry integrations, and
VTherm's config flow is an interactive multi-step menu with no import step, so
neither can be declared in ``configuration.yaml``. Rather than leave two manual
UI steps between a fresh container and a running controller, this creates the
entries through Home Assistant's own config-entry API.

Deliberately not done by writing ``.storage/core.config_entries`` by hand: the
on-disk format carries HA-version-specific bookkeeping, so hand-written JSON
rots silently. Here HA does the serialisation and we only supply the data, and
the VTherm keys come from importing VTherm's own ``const`` module -- so an
upstream rename fails loudly at import instead of producing a subtly wrong entry.

Idempotent: an existing entry for the same underlying is left alone, so
experiments made in the UI survive a restart.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

DOMAIN = "dev_bootstrap"
VTHERM_DOMAIN = "versatile_thermostat"
MPC_FAN_DOMAIN = "vtherm_mpc_fan"

UNDERLYING_CLIMATE = "climate.mock_ac"
ROOM_SENSOR = "sensor.room_temp"
OUTDOOR_SENSOR = "sensor.outdoor_temp"
VTHERM_NAME = "Mock Room"

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)


def _vtherm_data() -> dict[str, Any]:
    """Build the over_climate entry data, keyed by VTherm's own constants."""
    from custom_components.versatile_thermostat import const as vt  # noqa: PLC0415

    return {
        vt.CONF_NAME: VTHERM_NAME,
        vt.CONF_THERMOSTAT_TYPE: vt.CONF_THERMOSTAT_CLIMATE,
        vt.CONF_TEMP_SENSOR: ROOM_SENSOR,
        vt.CONF_EXTERNAL_TEMP_SENSOR: OUTDOOR_SENSOR,
        vt.CONF_CYCLE_MIN: 5,
        vt.CONF_DEVICE_POWER: 1.0,
        vt.CONF_USE_MAIN_CENTRAL_CONFIG: False,
        vt.CONF_USE_CENTRAL_MODE: False,
        vt.CONF_TEMP_MIN: 15.0,
        vt.CONF_TEMP_MAX: 35.0,
        vt.CONF_STEP_TEMPERATURE: 0.1,
        vt.CONF_UNDERLYING_LIST: [UNDERLYING_CLIMATE],
        # The mock AC cools, and this bench is about cooling.
        vt.CONF_AC_MODE: True,
        vt.CONF_AUTO_REGULATION_MODE: vt.CONF_AUTO_REGULATION_NONE,
        vt.CONF_AUTO_REGULATION_DTEMP: 0.5,
        vt.CONF_AUTO_REGULATION_PERIOD_MIN: 2,
        vt.CONF_AUTO_REGULATION_USE_DEVICE_TEMP: False,
        # Off on purpose: this plugin owns the fan. VTherm's own auto-fan would
        # fight it for the actuator.
        vt.CONF_AUTO_FAN_MODE: vt.CONF_AUTO_FAN_NONE,
        vt.CONF_USE_WINDOW_FEATURE: False,
        vt.CONF_USE_MOTION_FEATURE: False,
        vt.CONF_USE_POWER_FEATURE: False,
        vt.CONF_USE_PRESENCE_FEATURE: False,
        # Safety block. Present because entries added through the config-entry
        # API skip the config flow, so nothing fills in the defaults the UI
        # would have supplied; VTherm reads these straight off entry.data.
        # Values match VTherm's own test fixtures.
        vt.CONF_SAFETY_DELAY_MIN: 5,
        vt.CONF_SAFETY_MIN_ON_PERCENT: 0.4,
        vt.CONF_SAFETY_DEFAULT_ON_PERCENT: 0.3,
    }


def _new_entry(domain: str, title: str, data: dict[str, Any], version: int, minor: int) -> ConfigEntry:
    """Build a config entry HA will persist itself."""
    return ConfigEntry(
        version=version,
        minor_version=minor,
        domain=domain,
        title=title,
        data=data,
        source="user",
        options={},
        unique_id=None,
        discovery_keys={},
        subentries_data=(),
        state=ConfigEntryState.NOT_LOADED,
    )


def _find_vtherm(hass: HomeAssistant) -> ConfigEntry | None:
    """Return the bench VTherm entry if it already exists."""
    from custom_components.versatile_thermostat import const as vt  # noqa: PLC0415

    for entry in hass.config_entries.async_entries(VTHERM_DOMAIN):
        if UNDERLYING_CLIMATE in (entry.data.get(vt.CONF_UNDERLYING_LIST) or []):
            return entry
    return None


async def _async_bootstrap(hass: HomeAssistant) -> None:
    """Create the two entries if they are not there yet."""
    try:
        from custom_components.versatile_thermostat.const import (  # noqa: PLC0415
            CONFIG_MINOR_VERSION,
            CONFIG_VERSION,
        )
    except ImportError:
        _LOGGER.error(
            "Versatile Thermostat is not installed; run scripts/install_vtherm.sh"
        )
        return

    vtherm_entry = _find_vtherm(hass)
    if vtherm_entry is None:
        vtherm_entry = _new_entry(
            VTHERM_DOMAIN,
            VTHERM_NAME,
            _vtherm_data(),
            CONFIG_VERSION,
            CONFIG_MINOR_VERSION,
        )
        await hass.config_entries.async_add(vtherm_entry)
        _LOGGER.warning(
            "dev_bootstrap: created VTherm '%s' over %s", VTHERM_NAME, UNDERLYING_CLIMATE
        )
    else:
        _LOGGER.info("dev_bootstrap: VTherm over %s already exists", UNDERLYING_CLIMATE)

    # This plugin keys on the *thermostat's* unique_id, which VTherm derives as
    # the config entry id (see its climate.py).
    target = vtherm_entry.entry_id

    from custom_components.vtherm_mpc_fan.const import (  # noqa: PLC0415
        CONF_FAN_MODE_ORDER,
        CONF_TARGET_VTHERM,
    )

    existing = [
        entry
        for entry in hass.config_entries.async_entries(MPC_FAN_DOMAIN)
        if entry.data.get(CONF_TARGET_VTHERM) == target
    ]
    if existing:
        _LOGGER.info("dev_bootstrap: MPC fan entry already exists")
        return

    await hass.config_entries.async_add(
        _new_entry(
            MPC_FAN_DOMAIN,
            f"MPC Fan – {VTHERM_NAME}",
            {
                CONF_TARGET_VTHERM: target,
                "deadband": 0.2,
                "min_interval": 10,
                "data_collection": True,
                # Weakest to strongest. The mock AC reports them in this order
                # anyway, but setting it explicitly is what the bench is for.
                CONF_FAN_MODE_ORDER: ["silent", "low", "medium", "high", "turbo"],
            },
            1,
            1,
        )
    )
    _LOGGER.warning("dev_bootstrap: attached the MPC fan controller to '%s'", VTHERM_NAME)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Schedule the bootstrap once Home Assistant has started.

    Waiting for the started event matters: VTherm binds to `climate.mock_ac` and
    to the two template sensors, none of which exist while the YAML platforms are
    still being set up.
    """
    del config

    async def _run(_event) -> None:
        try:
            await _async_bootstrap(hass)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("dev_bootstrap failed")

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _run)
    return True
