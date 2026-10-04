"""Factory registering the MPC fan feature manager with the VTherm API."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .log import get_logger
from .const import (
    CONF_TARGET_VTHERM,
    CONF_THERMOSTAT_CLIMATE,
    CONF_THERMOSTAT_TYPE,
    DOMAIN,
    FEATURE_MANAGER_MPC_FAN,
)
from .manager import MpcFanFeatureManager

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from vtherm_api.interfaces import (
        InterfaceFeatureManager,
        InterfaceThermostatRuntime,
    )

_LOGGER = get_logger(__name__)


class MpcFanManagerFactory:
    """Create MPC fan managers for the VTherms this plugin is configured for.

    VTherm calls :meth:`supports` for every thermostat it sets up, so this is
    where the plugin declares its scope. Two conditions must hold: the thermostat
    must be an ``over_climate`` (only those proxy a fan-capable underlying), and
    the user must have created a config entry pointing at that specific VTherm --
    unlike VTherm's built-in managers, an external plugin should stay dormant on
    thermostats its owner never opted in.
    """

    def __init__(self, hass: "HomeAssistant") -> None:
        self._hass = hass

    @property
    def name(self) -> str:
        """Return the feature manager identifier."""
        return FEATURE_MANAGER_MPC_FAN

    def supports(self, thermostat: "InterfaceThermostatRuntime") -> bool:
        """Return True when this thermostat is an opted-in over_climate VTherm."""
        try:
            entry_infos = thermostat.entry_infos
        except Exception:  # pylint: disable=broad-except
            return False

        if isinstance(entry_infos, dict):
            if entry_infos.get(CONF_THERMOSTAT_TYPE) != CONF_THERMOSTAT_CLIMATE:
                return False
        elif getattr(thermostat, "underlying_fan_modes", None) is None:
            # Fallback for a runtime that does not expose its raw config.
            return False

        return self._is_configured_for(thermostat)

    def _is_configured_for(self, thermostat: "InterfaceThermostatRuntime") -> bool:
        """Return True when a config entry targets this VTherm."""
        config_entries = getattr(self._hass, "config_entries", None)
        if config_entries is None:
            return False
        try:
            entries = config_entries.async_entries(DOMAIN)
        except Exception:  # pylint: disable=broad-except
            return False
        return any(entry.data.get(CONF_TARGET_VTHERM) == thermostat.unique_id for entry in entries)

    def create(
        self,
        thermostat: "InterfaceThermostatRuntime",
    ) -> "InterfaceFeatureManager":
        """Create a manager bound to the runtime thermostat."""
        _LOGGER.info("Creating MPC fan manager for VTherm %s", thermostat.name)
        return MpcFanFeatureManager(thermostat, thermostat.hass)
