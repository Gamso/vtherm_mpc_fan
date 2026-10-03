"""MPC fan feature manager for Versatile Thermostat over_climate thermostats.

This manager owns the control cycle that used to be a standalone polling loop.
VTherm calls :meth:`refresh_state` once per cycle, right after it has recomputed
its regulated setpoint, and everything the cycle needs -- temperatures, slope,
hvac mode, the underlying's fan modes, whether the compressor is actually
running -- is read from the runtime instead of being scraped from entity
attributes. The decision logic itself (``MPCController``) and the learned model
(``ThermalLearning``) are unchanged: they operate on values, not on Home
Assistant objects.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any, TYPE_CHECKING

from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_DATA_COLLECTION,
    CONF_DEADBAND,
    CONF_DEFROST_ENTITY,
    CONF_FAN_MODE_ORDER,
    CONF_FIXED_FAN_HVAC_MODES,
    CONF_FIXED_FAN_SPEED,
    CONF_MIN_INTERVAL,
    CONF_TARGET_VTHERM,
    DEFAULT_CYCLE_MINUTES,
    DEFAULT_DATA_COLLECTION,
    DEFAULT_DEADBAND,
    DEFAULT_MIN_INTERVAL,
    DOMAIN,
    FEATURE_MANAGER_MPC_FAN,
    HVAC_OFF_REASON_WINDOW,
    MIN_ESTABLISHED_RATIO,
    PHASE_ESTABLISHED,
    PROFILE_HVAC_MODES,
    SETPOINT_DROP_LEARNING_COOLDOWN,
    SLOPE_SAMPLE_MIN_DELTA,
    STORAGE_KEY,
    STORAGE_VERSION,
    THRESHOLD_SLOPE,
    THRESHOLD_TARGET_DROP,
)
from .data_collection import DataCollector
from .mpc_controller import MPCController
from .registry import (
    add_entities_registry,
    entity_bucket,
    find_conflicting_plugin,
    managers,
)
from .number import PLATFORM_NUMBER, build_entities as build_number_entities
from .sensor import PLATFORM_SENSOR, build_entities as build_sensor_entities
from .thermal_learning import ThermalLearning

if TYPE_CHECKING:
    from vtherm_api.interfaces import InterfaceThermostatRuntime

_LOGGER = logging.getLogger(__name__)

ATTR_MPC_FAN_SECTION = "mpc_fan"

#: Statuses where the MPC declines to steer and the current fan must be held.
MPC_PAUSED_STATUSES = frozenset({"Idle", "Disturbed", "Not ready"})

#: Statuses where the fan is set, or held, by something other than the MPC's
#: own regulation: the force_fan override, the fixed-speed pin, or no ladder yet.
NOT_MPC_DRIVEN_STATUSES = MPC_PAUSED_STATUSES | {"Forced", "Fixed", "Unavailable"}

#: Defrost is inferred from an external entity and then held for this long, since
#: the heat pump keeps recovering after the flag itself clears.
DEFROST_COOLDOWN_MINUTES = 20.0

_TRUTHY = ("on", "true", "True", "1")

#: Actions the underlying can report that mean it is not producing right now.
_IDLE_ACTIONS = (HVACAction.IDLE, HVACAction.OFF)


def apply_configured_fan_order(
    detected: list[str], configured: list[str] | None
) -> list[str]:
    """Reorder *detected* fan modes to follow the user's weakest-to-strongest order.

    A fan mode's index is treated as its strength throughout the controller: the
    energy term, the step-down guard, the slope-ordering enforcement and the
    fallback for unlearned profiles all read it. VTherm reports the underlying's
    own order, which is usually right but is not guaranteed, so the plugin lets
    it be overridden.

    Modes detected but not listed (added by the underlying after configuration)
    are appended so they stay usable; listed modes that no longer exist are
    dropped.
    """
    if not configured:
        return detected
    ordered = [mode for mode in configured if mode in detected]
    ordered += [mode for mode in detected if mode not in ordered]
    return ordered


@dataclass(slots=True)
class FanOverride:
    """A manual fan pin set by the ``force_fan`` service.

    A small type rather than a raw dict: the fields are read on the control path
    where a typo or a missing key would only surface at runtime, and the
    optional-attribute shape (``FanOverride | None``) is what tells a reader the
    override can expire underneath them.
    """

    fan_mode: str
    until: float  # epoch seconds after which the override lapses


def build_data_collection_decision(
    *,
    effective_fan: str | None,
    effective_reason: str,
    current_fan: str | None,
    current_error: float,
    minutes_since_change: float,
    hvac_mode: str,
    target_temp: float,
    mpc_decision: dict,
) -> dict:
    """Build the audit payload written to the CSV collector."""
    projected_temperature = mpc_decision.get("mpc_predicted_temperature_10m")
    projected_error = None

    if projected_temperature is not None:
        projected_error = (
            projected_temperature - target_temp
            if hvac_mode == "cool"
            else target_temp - projected_temperature
        )

    return {
        "fan_mode": effective_fan,
        "reason": effective_reason,
        "current_fan": current_fan,
        "temperature_error": current_error,
        "projected_temperature": projected_temperature,
        "projected_temperature_error": projected_error,
        "minutes_since_last_change": minutes_since_change,
    }


def filter_supported_fan_modes(raw_modes: list[str] | None) -> list[str]:
    """Keep only manual fan modes; ``auto``/``off`` are not strength levels."""
    if not raw_modes:
        return []
    return [
        mode
        for mode in raw_modes
        if isinstance(mode, str) and mode.lower() not in {"auto", "off"}
    ]


class MpcFanFeatureManager:
    """Drive the underlying climate's fan mode from the learned MPC model."""

    #: Recorder can only filter top-level keys, so every attribute this manager
    #: publishes lives under one section, declared here.
    unrecorded_attributes = frozenset({ATTR_MPC_FAN_SECTION})

    def __init__(self, thermostat: "InterfaceThermostatRuntime", hass: HomeAssistant):
        self._vtherm = thermostat
        self._hass = hass
        self._name = thermostat.name

        self._learning = ThermalLearning()
        self._mpc: MPCController | None = None
        self._store: Store | None = None
        self._collector: DataCollector | None = None
        self._loaded = False

        # Control-cycle memory (previously the module-level ctrl_state dict).
        self._last_change_time: float = 0.0
        self._previous_slope: float | None = None
        self._last_hvac_mode: str | None = None
        # When this manager ran its first cycle. A fresh manager (restart, VTherm
        # reload, options change) has no fan-change history, so the fixed-speed
        # pin measures its min interval from here instead of re-applying at once.
        self._first_cycle_time: float | None = None
        self._last_setpoint_drop_time: float = 0.0
        self._defrost_active = False
        self._defrost_start_time: float = 0.0
        self._last_sent_fan_mode: str | None = None
        # Fan mode read from the climate entity on the previous cycle. A change
        # here that this manager did not command (remote control, automation,
        # another integration) is still a change: the dead time, the response
        # event and the learning gate all restart from it.
        self._last_observed_fan: str | None = None
        # One response event per fan change: armed by a change, consumed by the
        # first significant slope move after it. Without this every slope jump
        # inside the 60-minute window counted as another response to the same
        # change, and the median dead time drifted toward the middle of the window.
        self._response_armed = False
        # Last slope actually handed to the model per (hvac_mode, fan_mode).
        # Not persisted: after a restart the first sample of each mode is
        # accepted, which costs at most one duplicate and avoids stale state.
        self._last_sampled_slope: dict[tuple[str, str], float] = {}

        #: Manual override set by the force_fan service: {"fan_mode": str, "until": float}
        self.force: FanOverride | None = None

        self._last_decision: dict[str, Any] = {}
        self._active_listener: list = []
        #: Domain of another fan-driving plugin on this VTherm, when one is found.
        self._conflict: str | None = None
        self._conflict_logged = False

    # ------------------------------------------------------------------
    # Lifecycle (InterfaceFeatureManager contract)
    # ------------------------------------------------------------------
    def post_init(self, entry_infos: Any) -> None:
        """Publish the manager so entity platforms and services can find it."""
        del entry_infos
        managers(self._hass)[self._vtherm.unique_id] = self
        _LOGGER.info("MPC fan plugin registered for VTherm %s", self._vtherm.unique_id)

    async def start_listening(self, force: bool = False) -> None:
        """Load the persisted model and build the controller."""
        del force
        self.stop_listening()
        await self._async_ensure_loaded()

    def stop_listening(self) -> bool | None:
        """Remove every listener registered through :meth:`add_listener`."""
        while self._active_listener:
            self._active_listener.pop()()

    async def refresh_state(self) -> bool:
        """Run one control cycle. Returns True when the fan mode was changed."""
        await self._async_ensure_loaded()
        # Retried every cycle: the sensor platform may set up after this manager,
        # and the fan ladder can grow once the underlying publishes its modes.
        self.ensure_entities()
        return await self._async_run_cycle()

    def ensure_entities(self) -> None:
        """Create this plugin's entities once their platform callbacks are up.

        Each platform (sensor, number) sets up on Home Assistant's own
        schedule and publishes its ``async_add_entities`` callback to the
        registry independently, so they are checked independently here too --
        one arriving before the other must not block the other from ever
        being built.
        """
        registry = add_entities_registry(self._hass).get(self._vtherm.unique_id)
        if not registry or self._mpc is None:
            return

        for platform, build in (
            (PLATFORM_SENSOR, build_sensor_entities),
            (PLATFORM_NUMBER, build_number_entities),
        ):
            add_entities = registry.get(platform)
            if add_entities is None:
                continue
            new_entities = build(self)
            if new_entities:
                add_entities(new_entities)
                _LOGGER.info(
                    "Created %d %s entities for %s", len(new_entities), platform, self._name
                )

    def restore_state(self, old_state: Any) -> None:
        """No-op: the learned model is restored from this plugin's own Store."""
        del old_state

    def add_listener(self, func) -> None:
        """Register a callback to be removed on stop."""
        self._active_listener.append(func)

    def add_custom_attributes(self, extra_state_attributes: dict[str, Any]) -> None:
        """Expose the MPC diagnostics inside the VTherm's own state attributes."""
        extra_state_attributes[ATTR_MPC_FAN_SECTION] = {
            "fan_mode_order": list(self.fan_modes or []),
            "sent_fan_mode": self._last_sent_fan_mode,
            "learning_ready": self._learning.is_ready(),
            "forced_until": self.force.until if self.force else None,
            # Non-null means another plugin owns the fan and this one is standing
            # down; surfaced here so a silent yield is still visible.
            "conflicting_plugin": self._conflict,
            **{
                key: value
                for key, value in self._last_decision.items()
                if key.startswith("mpc_")
            },
        }

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        """Logical name of the manager."""
        return FEATURE_MANAGER_MPC_FAN

    @property
    def hass(self) -> HomeAssistant:
        """Home Assistant instance."""
        return self._hass

    @property
    def vtherm(self) -> "InterfaceThermostatRuntime":
        """The runtime thermostat bound to this manager."""
        return self._vtherm

    @property
    def vtherm_unique_id(self) -> str:
        """The unique_id of the bound VTherm."""
        return self._vtherm.unique_id

    @property
    def vtherm_name(self) -> str:
        """The name of the bound VTherm."""
        return self._name

    @property
    def learning(self) -> ThermalLearning:
        """The learned thermal model."""
        return self._learning

    @property
    def mpc(self) -> MPCController | None:
        """The MPC controller, once the model has been loaded."""
        return self._mpc

    @property
    def fan_modes(self) -> list[str] | None:
        """The fan-mode ladder in use, weakest first."""
        return self._mpc.fan_modes if self._mpc else None

    @property
    def last_decision(self) -> dict[str, Any]:
        """The most recent MPC decision, for entity consumption."""
        return self._last_decision

    @property
    def is_configured(self) -> bool:
        """True once the controller exists and the underlying exposes fan modes."""
        return self._mpc is not None and bool(self._vtherm.underlying_fan_modes)

    @property
    def is_detected(self) -> bool:
        """True when the MPC is actively steering (not paused, forced or pinned).

        This is the ``InterfaceFeatureManager`` "condition detected" flag: for
        this plugin, that the fan speed is currently the MPC's decision. A
        force_fan override or a fixed-speed pin also sets the fan, but neither
        is regulation, so neither counts.
        """
        status = self._last_decision.get("mpc_status")
        return bool(status) and status not in NOT_MPC_DRIVEN_STATUSES

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def _config(self) -> dict[str, Any]:
        """Return the merged config of the entry targeting this VTherm."""
        config_entries = getattr(self._hass, "config_entries", None)
        if config_entries is None:
            return {}
        try:
            entries = config_entries.async_entries(DOMAIN)
        except Exception:  # pylint: disable=broad-except
            return {}
        for entry in entries:
            if entry.data.get(CONF_TARGET_VTHERM) == self._vtherm.unique_id:
                return {**entry.data, **entry.options}
        return {}

    def _entry_id(self) -> str:
        """Return the config entry id targeting this VTherm, or the VTherm's id."""
        config_entries = getattr(self._hass, "config_entries", None)
        if config_entries is not None:
            try:
                for entry in config_entries.async_entries(DOMAIN):
                    if entry.data.get(CONF_TARGET_VTHERM) == self._vtherm.unique_id:
                        return entry.entry_id
            except Exception:  # pylint: disable=broad-except
                pass
        return self._vtherm.unique_id

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    async def _async_ensure_loaded(self) -> None:
        """Load the persisted model and build the controller, once."""
        if self._loaded:
            self._sync_fan_modes()
            return

        conf = self._config()
        entry_id = self._entry_id()

        self._store = Store(self._hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry_id}")
        data = await self._store.async_load()
        if data:
            self._learning = ThermalLearning.from_dict(data)
            _LOGGER.info(
                "Restored learning data for %s (%d slope samples, %d response events)",
                self._name,
                len(data.get("slope_samples", [])),
                len(data.get("response_events", [])),
            )

        self._mpc = MPCController(
            learning=self._learning,
            deadband=conf.get(CONF_DEADBAND, DEFAULT_DEADBAND),
            min_interval=conf.get(CONF_MIN_INTERVAL, DEFAULT_MIN_INTERVAL),
            fan_modes=self._resolve_fan_modes(conf) or None,
            # The MPC simulates in control-cycle steps, so it must use the cadence
            # it is actually driven at -- which VTherm owns, not this plugin.
            cycle_minutes=self._vtherm.cycle_min or DEFAULT_CYCLE_MINUTES,
        )

        if conf.get(CONF_DATA_COLLECTION, DEFAULT_DATA_COLLECTION):
            self._collector = DataCollector(
                self._hass, self._hass.config.config_dir, entry_id
            )
            await self._collector.async_initialize()
            _LOGGER.info("Data collection enabled, writing to %s", self._collector.path)

        self._loaded = True

    async def async_save(self) -> None:
        """Persist the learned model."""
        if self._store is not None:
            await self._store.async_save(self._learning.to_dict())

    def _resolve_fan_modes(self, conf: dict[str, Any] | None = None) -> list[str]:
        """Return the ordered fan-mode ladder from the underlying."""
        conf = self._config() if conf is None else conf
        detected = filter_supported_fan_modes(self._vtherm.underlying_fan_modes)
        return apply_configured_fan_order(detected, conf.get(CONF_FAN_MODE_ORDER))

    def _sync_fan_modes(self) -> None:
        """Adopt the underlying's fan modes once they become available.

        An ``over_climate`` underlying may not have published its ``fan_modes``
        when the VTherm starts, so this is retried every cycle and the ladder
        self-heals rather than staying empty for the session.
        """
        if self._mpc is None:
            return
        resolved = self._resolve_fan_modes()
        if resolved and resolved != self._mpc.fan_modes:
            self._mpc.fan_modes = resolved
            _LOGGER.info("Fan mode ladder for %s: %s", self._name, resolved)

    # ------------------------------------------------------------------
    # Disturbances
    # ------------------------------------------------------------------
    def _is_window_open(self) -> bool:
        """True when VTherm stopped the underlying because a window is open."""
        return self._vtherm.hvac_off_reason == HVAC_OFF_REASON_WINDOW

    def _underlying_hvac_action(self) -> str | None:
        """Return the underlying climate's own hvac_action, or None if it has none.

        VTherm's ``is_device_active`` looks like the natural source here, but it
        cannot be trusted: when the underlying publishes no ``hvac_action``,
        VTherm synthesises one from a bare target-vs-current sign check (its
        "Issue 1779" fallback). That synthetic value reads IDLE across the whole
        "at or past setpoint" region -- exactly the equilibrium this controller
        exists to hold, and exactly when the unit is usually still running. So
        the device's own attribute is read instead, and when the hardware does
        not publish one the answer is None: unknown, rather than a guess.
        """
        getter = getattr(self._vtherm, "underlying_entity_id", None)
        if getter is None:
            return None
        entity_id = getter(0)
        if not entity_id:
            return None

        state = self._hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        # A climate entity that is off is off -- no simulation involved.
        if state.state == HVACMode.OFF:
            return HVACAction.OFF
        return state.attributes.get("hvac_action") or None

    def _is_hvac_idle(self) -> bool:
        """True when the underlying reports it is not producing.

        An underlying that publishes no ``hvac_action`` yields False rather than
        True: not knowing must not freeze fan control, and the only alternative
        source is wrong precisely at equilibrium (see
        :meth:`_underlying_hvac_action`).
        """
        return self._underlying_hvac_action() in _IDLE_ACTIONS

    def _is_defrost_active(self) -> bool:
        """True while defrost is reported, held for a cooldown after it clears.

        Two sources feed this. The underlying's own ``hvac_action`` is preferred:
        a device that reports ``defrosting`` needs no configuration at all. The
        optional defrost entity remains the fallback for the devices that report
        nothing -- which is why it cannot be the only source.
        """
        now_ts = time.time()
        source: str | None = None

        if self._underlying_hvac_action() == HVACAction.DEFROSTING:
            source = "Underlying climate hvac_action"

        entity_id = self._config().get(CONF_DEFROST_ENTITY)
        if source is None and entity_id:
            state = self._hass.states.get(entity_id)
            if state is not None and state.state in _TRUTHY:
                source = f"Defrost entity {entity_id}"

        if source is not None:
            if not self._defrost_active:
                _LOGGER.info("%s reports active defrost", source)
            self._defrost_active = True
            self._defrost_start_time = now_ts

        if self._defrost_active:
            elapsed = (now_ts - self._defrost_start_time) / 60.0
            if elapsed > DEFROST_COOLDOWN_MINUTES:
                self._defrost_active = False
                _LOGGER.debug("Defrost cooldown expired after %.1f min", elapsed)

        return self._defrost_active

    # ------------------------------------------------------------------
    # Force override
    # ------------------------------------------------------------------
    def _resolve_active_force(self, now: float) -> tuple[str, float] | None:
        """Return ``(fan_mode, deadline)`` while an override is active, else None.

        The deadline is returned rather than left for the caller to read back off
        ``self.force``: this method clears the override on expiry, so a caller
        reaching into the attribute afterwards would be relying on an invariant
        it cannot see.

        Clears expired or now-invalid overrides so control returns to the MPC.
        """
        force = self.force
        if force is None:
            return None

        if now >= force.until:
            self.force = None
            _LOGGER.info("Force fan expired for %s; resuming MPC control", self._name)
            return None

        fan_modes = self.fan_modes
        if fan_modes and force.fan_mode not in fan_modes:
            self.force = None
            _LOGGER.warning(
                "Forced fan '%s' is no longer valid for %s; cancelling override",
                force.fan_mode,
                self._name,
            )
            return None

        return force.fan_mode, force.until

    # ------------------------------------------------------------------
    # Learning gate
    # ------------------------------------------------------------------
    def _should_collect_slope_sample(
        self,
        *,
        current_fan: str | None,
        hvac_mode: str,
        is_defrost_active: bool,
        is_hvac_idle: bool,
        phase: str,
        minutes_since_change: float,
        learned_dead_time: float,
        now: float,
    ) -> bool:
        """Return True when slope-learning conditions are met."""
        # Only heating/cooling produce meaningful thermal-response samples; skip
        # off/dry/fan_only so those cycles don't pollute the learned profiles.
        if hvac_mode not in ("heat", "cool"):
            return False
        if current_fan is None or is_defrost_active or is_hvac_idle:
            return False
        if phase != PHASE_ESTABLISHED:
            return False
        if minutes_since_change < learned_dead_time * MIN_ESTABLISHED_RATIO:
            return False
        if self._last_setpoint_drop_time != 0:
            since_drop = (now - self._last_setpoint_drop_time) / 60.0
            if since_drop < SETPOINT_DROP_LEARNING_COOLDOWN:
                return False
        return True

    def _check_conflict(self) -> str | None:
        """Return the domain of a competing fan controller on this VTherm, else None.

        Re-checked every cycle because the other plugin can be installed after
        this one. When one is found this manager yields the actuator instead of
        fighting for it: the competing plugin is the one the user just chose, and
        a fan flapping between two opinions is worse than either opinion alone.
        Learning and diagnostics keep running, so the yield is observable and
        reverses cleanly once the conflict is removed.
        """
        conflict = find_conflicting_plugin(self._hass, self._vtherm.unique_id)

        if conflict and not self._conflict_logged:
            _LOGGER.error(
                "%s - '%s' is also configured to drive the fan of this VTherm. "
                "Standing down: no fan command will be sent while both are active. "
                "Remove one of the two to restore MPC control",
                self,
                conflict,
            )
            self._conflict_logged = True
        elif not conflict and self._conflict_logged:
            _LOGGER.info("%s - fan conflict resolved; resuming MPC control", self)
            self._conflict_logged = False

        self._conflict = conflict
        return conflict

    def _is_duplicate_slope(self, fan_mode: str, hvac_mode: str, slope: float) -> bool:
        """True when this reading merely repeats the last one taken for that mode.

        VTherm recomputes its slope only when the room sensor publishes a new
        value, so several control cycles in a row can read the exact same
        number. Feeding it to the model each time records one measurement as
        many: MIN_MODE_PROFILE_SAMPLES counts rows, so duplicates let a
        rarely-used speed clear the reliability gate on a handful of genuine
        observations and then be trusted as if it had ten. Measured on a 15-day
        production trace, 89% of consecutive 2-minute readings were repeats.
        """
        key = (hvac_mode, fan_mode)
        last = self._last_sampled_slope.get(key)
        if last is not None and abs(slope - last) < SLOPE_SAMPLE_MIN_DELTA:
            return True
        self._last_sampled_slope[key] = slope
        return False

    # ------------------------------------------------------------------
    # Control cycle
    # ------------------------------------------------------------------
    def _read_inputs(self) -> tuple[float, float, float, str, str | None] | None:
        """Return (slope, current_temp, target_temp, hvac_mode, current_fan).

        Returns None when the runtime cannot supply usable numbers -- VTherm can
        briefly expose None during restarts, and a skipped cycle is preferable to
        feeding the model a guess.
        """
        vtherm = self._vtherm
        current_temp = vtherm.current_temperature
        target_temp = vtherm.regulated_target_temperature
        if target_temp is None:
            target_temp = vtherm.target_temperature
        slope = vtherm.last_temperature_slope
        hvac_mode = vtherm.vtherm_hvac_mode

        if current_temp is None or target_temp is None:
            _LOGGER.debug(
                "Skipping cycle for %s: incomplete temperatures (current=%s, target=%s)",
                self._name,
                current_temp,
                target_temp,
            )
            return None

        try:
            slope_value = float(slope if slope is not None else 0.0)
            current_value = float(current_temp)
            target_value = float(target_temp)
        except (TypeError, ValueError):
            _LOGGER.debug("Skipping cycle for %s: non-numeric runtime data", self._name)
            return None

        current_fan = self._current_fan_mode()
        return slope_value, current_value, target_value, str(hvac_mode), current_fan

    def _current_fan_mode(self) -> str | None:
        """Return the fan mode currently set on the underlying climate."""
        state = self._hass.states.get(self._vtherm.entity_id)
        if state is None:
            return self._last_sent_fan_mode
        return state.attributes.get("fan_mode") or self._last_sent_fan_mode

    async def _async_run_cycle(self) -> bool:
        """Evaluate the MPC and apply its decision. Returns True on a fan change."""
        if self._mpc is None:
            return False
        self._sync_fan_modes()
        if not self._mpc.fan_modes:
            _LOGGER.debug("No fan modes available yet for %s", self._name)
            return False

        inputs = self._read_inputs()
        if inputs is None:
            return False
        vtherm_slope, current_temp, target_temp, hvac_mode, current_fan = inputs

        is_window_open = self._is_window_open()
        is_defrost_active = self._is_defrost_active()
        is_hvac_idle = self._is_hvac_idle()

        now = time.time()
        if self._first_cycle_time is None:
            self._first_cycle_time = now
        if self._is_external_fan_change(current_fan):
            _LOGGER.info(
                "%s - fan mode changed to '%s' outside this plugin; restarting the dead time",
                self._name,
                current_fan,
            )
            self._register_fan_change(now)
        self._last_observed_fan = current_fan
        minutes_since_change = (
            (now - self._last_change_time) / 60.0 if self._last_change_time else 1e6
        )

        # Reset slope memory on HVAC mode switch: a heating slope tells us
        # nothing about the cooling response and vice versa. The first cycle of
        # a manager is not a switch: the previous mode is unknown, not different.
        hvac_mode_entered = self._last_hvac_mode is not None and self._last_hvac_mode != hvac_mode
        if hvac_mode_entered:
            _LOGGER.info(
                "HVAC mode changed %s -> %s: resetting slope memory",
                self._last_hvac_mode,
                hvac_mode,
            )
            self._previous_slope = None
            # A pending response belongs to the mode its fan change was made in.
            self._response_armed = False
        self._last_hvac_mode = hvac_mode

        if self._previous_slope is None:
            self._previous_slope = vtherm_slope
        slope_change = abs(vtherm_slope - self._previous_slope) > THRESHOLD_SLOPE

        # Signed comfort error (positive = needs more heating/cooling).
        current_error = (
            (current_temp - target_temp)
            if hvac_mode == "cool"
            else (target_temp - current_temp)
        )
        # Only a regulated mode has an error direction. Read in dry or fan_only
        # with the heating convention, a warm summer room (27 C for a 24 C
        # setpoint) looked like a large setpoint drop on every cycle and blocked
        # learning for 30 min after switching to cool.
        if hvac_mode in PROFILE_HVAC_MODES and current_error < THRESHOLD_TARGET_DROP:
            self._last_setpoint_drop_time = now

        decision = self._mpc.evaluate(
            current_temp=current_temp,
            target_temp=target_temp,
            vtherm_slope=vtherm_slope,
            hvac_mode=hvac_mode,
            current_fan=current_fan,
            is_window_open=is_window_open,
            is_defrost_active=is_defrost_active,
            is_hvac_idle=is_hvac_idle,
            minutes_since_change=minutes_since_change,
        )

        active_force = self._resolve_active_force(now)
        fixed_fan = self._resolve_fixed_fan(hvac_mode)
        if active_force is not None:
            forced_fan, deadline = active_force
            remaining_min = (deadline - now) / 60.0
            effective_fan = forced_fan
            effective_reason = f"Forced fan '{forced_fan}' ({remaining_min:.0f} min left)"
            decision = {
                **decision,
                "mpc_status": "Forced",
                "mpc_fan_mode": forced_fan,
                "mpc_reason": effective_reason,
                "mpc_would_change_now": "yes" if forced_fan != current_fan else "no",
            }
        elif fixed_fan is not None:
            # Applied at once on entering the mode; afterwards the min interval
            # applies, so a manual change (which restarts it) is not reverted on
            # the very next cycle. A fresh manager has seen no change yet, so its
            # clock starts at its first cycle: otherwise every restart or reload
            # would overwrite a speed the user had set by hand.
            min_interval = self._config().get(CONF_MIN_INTERVAL, DEFAULT_MIN_INTERVAL)
            pin_clock = (
                minutes_since_change
                if self._last_change_time
                else (now - self._first_cycle_time) / 60.0
            )
            apply_pin = hvac_mode_entered or pin_clock >= min_interval
            effective_reason = f"Fixed fan '{fixed_fan}' for HVAC mode '{hvac_mode}'"
            hold_reason = self._fixed_fan_hold_reason(is_window_open, is_defrost_active)
            if hold_reason is not None:
                apply_pin = False
                effective_reason += f" (held: {hold_reason})"
            effective_fan = fixed_fan if apply_pin else current_fan
            decision = {
                **decision,
                "mpc_status": "Fixed",
                "mpc_fan_mode": fixed_fan,
                "mpc_reason": effective_reason,
                "mpc_would_change_now": (
                    "yes" if apply_pin and fixed_fan != current_fan else "no"
                ),
            }
        elif decision.get("mpc_status") not in MPC_PAUSED_STATUSES and decision.get(
            "mpc_fan_mode"
        ):
            effective_fan = decision["mpc_fan_mode"]
            effective_reason = f"MPC: {decision.get('mpc_reason', 'MPC')}"
        else:
            effective_fan = current_fan
            effective_reason = f"MPC paused: {decision.get('mpc_status', 'unknown')}"

        # Phase classification (gates learning and is recorded in the CSV). Same
        # dead time and same classifier as the MPC: gating this on is_ready()
        # left the learner on the 10-minute default while the controller was
        # working with a measured 24-30 minutes, so samples were taken inside the
        # real transient and labelled ESTABLISHED.
        learned_dead_time = self._learning.get_dead_time(hvac_mode)
        phase = MPCController.detect_phase(minutes_since_change, learned_dead_time)

        if self._should_collect_slope_sample(
            current_fan=current_fan,
            hvac_mode=hvac_mode,
            is_defrost_active=is_defrost_active,
            is_hvac_idle=is_hvac_idle,
            phase=phase,
            minutes_since_change=minutes_since_change,
            learned_dead_time=learned_dead_time,
            now=now,
        ) and not self._is_duplicate_slope(current_fan, hvac_mode, vtherm_slope):  # type: ignore[arg-type]
            self._learning.add_slope_sample(
                current_fan, vtherm_slope, current_error, hvac_mode, is_window_open  # type: ignore[arg-type]
            )

        if hvac_mode not in PROFILE_HVAC_MODES:
            # Dead time is the heating/cooling lag. In dry or fan_only the slope
            # moves for other reasons, and the pinned-speed commands would
            # otherwise feed it events of their own.
            self._response_armed = False
        elif slope_change and self._response_armed:
            response_time = minutes_since_change
            if response_time > 60.0:
                # Too late to be a response to the change: stop waiting for one.
                self._response_armed = False
            elif (
                response_time >= 2.0
                and not is_window_open
                and not is_defrost_active
                and not is_hvac_idle
            ):
                self._learning.add_response_event(response_time, hvac_mode)
                self._response_armed = False

        if slope_change:
            self._previous_slope = vtherm_slope

        self._last_decision = {
            **decision,
            "fan_mode": effective_fan,
            "minutes_since_last_change": round(minutes_since_change, 2),
        }

        await self._async_record(
            hvac_mode=hvac_mode,
            current_temp=current_temp,
            target_temp=target_temp,
            vtherm_slope=vtherm_slope,
            is_window_open=is_window_open,
            is_defrost_active=is_defrost_active,
            is_hvac_idle=is_hvac_idle,
            phase=phase,
            decision=decision,
            effective_fan=effective_fan,
            effective_reason=effective_reason,
            current_fan=current_fan,
            current_error=current_error,
            minutes_since_change=minutes_since_change,
            forced=active_force is not None,
        )

        self._push_to_entities()

        if self._check_conflict():
            # Everything above still ran, so the model keeps learning and the
            # sensors keep reporting what the MPC *would* do -- only the command
            # is withheld.
            return False

        if effective_fan is not None and effective_fan != current_fan:
            _LOGGER.info(
                "%s - setting underlying fan mode to '%s' (%s)",
                self._name,
                effective_fan,
                effective_reason,
            )
            await self._vtherm.async_set_underlying_fan_mode(effective_fan)
            self._last_sent_fan_mode = effective_fan
            self._register_fan_change(time.time())
            await self.async_save()
            return True

        return False

    def _resolve_fixed_fan(self, hvac_mode: str) -> str | None:
        """Return the speed pinned for *hvac_mode*, or None when no pin applies.

        A speed the underlying no longer offers is ignored, so a renamed mode
        degrades to "hold the current fan" rather than sending a command the
        device would reject.
        """
        conf = self._config()
        fixed_speed = conf.get(CONF_FIXED_FAN_SPEED)
        if not fixed_speed or hvac_mode not in (conf.get(CONF_FIXED_FAN_HVAC_MODES) or []):
            return None
        available = self._vtherm.underlying_fan_modes
        if available and fixed_speed not in available:
            _LOGGER.debug(
                "%s - fixed fan '%s' is not offered by the underlying; ignoring pin",
                self._name,
                fixed_speed,
            )
            return None
        return fixed_speed

    def _fixed_fan_hold_reason(self, is_window_open: bool, is_defrost_active: bool) -> str | None:
        """Return why the fixed speed must not be sent right now, or None.

        The pin is a plain command, not a regulation, but it still must not
        reach a unit that is stopped: some IR or cloud climates treat any
        ``set_fan_mode`` as a power-on, so re-applying the speed with a window
        open or the underlying switched off could restart it. Defrost is held
        too, like every other fan command. The current speed is kept and the
        pin resumes on the first cycle the disturbance has cleared.
        """
        if is_window_open:
            return "window open"
        if self._underlying_hvac_action() == HVACAction.OFF:
            return "underlying off"
        if is_defrost_active:
            return "defrost active"
        return None

    def _is_external_fan_change(self, current_fan: str | None) -> bool:
        """True when the fan mode moved since last cycle without this plugin asking."""
        if current_fan is None or self._last_observed_fan is None:
            return False
        return current_fan not in (self._last_observed_fan, self._last_sent_fan_mode)

    def _register_fan_change(self, now: float) -> None:
        """Restart everything that is measured from the last fan change."""
        self._last_change_time = now
        self._previous_slope = None
        self._response_armed = True
        if self._mpc is not None:
            self._mpc.notify_fan_change()

    async def _async_record(self, **kwargs) -> None:
        """Append one row to the data-collection CSV, when enabled."""
        if self._collector is None or self._mpc is None:
            return
        decision = kwargs["decision"]
        hvac_mode = kwargs["hvac_mode"]
        vtherm_slope = kwargs["vtherm_slope"]
        collector_decision = build_data_collection_decision(
            effective_fan=kwargs["effective_fan"],
            effective_reason=kwargs["effective_reason"],
            current_fan=kwargs["current_fan"],
            current_error=kwargs["current_error"],
            minutes_since_change=kwargs["minutes_since_change"],
            hvac_mode=hvac_mode,
            target_temp=kwargs["target_temp"],
            mpc_decision=decision,
        )
        await self._collector.async_record(
            hvac_mode=hvac_mode,
            current_temp=kwargs["current_temp"],
            target_temp=kwargs["target_temp"],
            vtherm_slope=vtherm_slope,
            is_window_open=kwargs["is_window_open"],
            decision=collector_decision,
            phase=kwargs["phase"],
            effective_slope=(
                -float(vtherm_slope) if hvac_mode == "cool" else float(vtherm_slope)
            ),
            effective_timeout=self._mpc.get_effective_timeout(hvac_mode),
            force=kwargs["forced"],
            learning_ready=self._learning.is_ready(),
            dead_time=self._learning.get_dead_time(hvac_mode),
            mpc_decision=decision,
            defrost_active=kwargs["is_defrost_active"],
            is_hvac_idle=kwargs["is_hvac_idle"],
            outdoor_temp=self._vtherm.current_outdoor_temperature,
        )

    def _push_to_entities(self) -> None:
        """Refresh this plugin's sensors, then the VTherm's own attributes."""
        bucket = entity_bucket(self._hass, self._vtherm.unique_id)
        for sensor in list(bucket["sensors"].values()) + list(bucket["profiles"].values()):
            if hasattr(sensor, "update_from_mpc"):
                sensor.update_from_mpc(self._last_decision)
            if sensor.hass is not None:
                sensor.async_write_ha_state()

        self._vtherm.update_custom_attributes()
        self._vtherm.async_write_ha_state()

    def __str__(self) -> str:
        """Readable representation used in logs."""
        return f"MpcFanManager-{self._name}"
