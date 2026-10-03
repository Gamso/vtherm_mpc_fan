"""Tests for the VTherm feature-manager wrapper around the MPC controller."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.vtherm_mpc_fan.manager import (
    FanOverride,
    MpcFanFeatureManager,
    apply_configured_fan_order,
    filter_supported_fan_modes,
)

FAN_MODES = ["silent", "low", "med", "high", "superhigh"]
UNDERLYING_ENTITY = "climate.living_room_ac"


def _make_runtime(**overrides):
    """Build a stand-in for VTherm's InterfaceThermostatRuntime."""
    runtime = MagicMock()
    runtime.unique_id = "vtherm-uid"
    runtime.entity_id = "climate.living_room"
    runtime.name = "Living room"
    runtime.entry_infos = {"thermostat_type": "thermostat_over_climate"}
    runtime.current_temperature = 24.0
    runtime.target_temperature = 22.0
    runtime.regulated_target_temperature = 22.0
    runtime.current_outdoor_temperature = 31.0
    runtime.last_temperature_slope = -0.8
    runtime.vtherm_hvac_mode = "cool"
    runtime.hvac_off_reason = None
    runtime.is_device_active = True
    runtime.underlying_entity_id = MagicMock(return_value=UNDERLYING_ENTITY)
    runtime.cycle_min = 5
    runtime.underlying_fan_modes = list(FAN_MODES)
    runtime.async_set_underlying_fan_mode = AsyncMock()
    runtime.update_custom_attributes = MagicMock()
    runtime.async_write_ha_state = MagicMock()
    for key, value in overrides.items():
        setattr(runtime, key, value)
    return runtime


def _make_hass(
    fan_mode: str = "low",
    hvac_action: str | None = "cooling",
    underlying_state: str = "cool",
    extra_states: dict | None = None,
):
    """Build a hass stub exposing the underlying climate's state.

    ``hvac_action=None`` models the devices at the heart of this plugin's idle
    detection: those that publish no action at all.
    """
    hass = MagicMock()
    hass.data = {}
    state = MagicMock()
    state.attributes = {"fan_mode": fan_mode}
    if hvac_action is not None:
        state.attributes["hvac_action"] = hvac_action
    state.state = underlying_state
    states = {UNDERLYING_ENTITY: state, **(extra_states or {})}
    hass.states.get = MagicMock(side_effect=lambda entity_id: states.get(entity_id, state))
    hass.config_entries.async_entries = MagicMock(return_value=[])
    hass.config.config_dir = "/tmp"
    return hass


def _make_store():
    """Return a Store stand-in: nothing persisted, saves accepted and discarded."""
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()
    return store


async def _build_manager(runtime=None, hass=None, **config):
    """Create a manager with data collection off and the model loaded."""
    runtime = runtime or _make_runtime()
    hass = hass or _make_hass()
    manager = MpcFanFeatureManager(runtime, hass)
    manager._config = MagicMock(return_value={"data_collection": False, **config})  # noqa: SLF001
    manager._entry_id = MagicMock(return_value="entry-1")  # noqa: SLF001
    manager.ensure_entities = MagicMock()
    with patch(
        "custom_components.vtherm_mpc_fan.manager.Store", return_value=_make_store()
    ):
        await manager.start_listening()
    return manager


def test_filter_supported_fan_modes_drops_auto_and_off() -> None:
    """auto/off are not strength levels and must never enter the ladder."""
    assert filter_supported_fan_modes(["Auto", "low", "high", "off"]) == ["low", "high"]
    assert filter_supported_fan_modes(None) == []


def test_apply_configured_fan_order_overrides_the_underlying() -> None:
    """The configured order wins over the order the underlying reports."""
    detected = ["high", "low", "superhigh", "med"]
    configured = ["low", "med", "high", "superhigh"]
    assert apply_configured_fan_order(detected, configured) == configured
    assert apply_configured_fan_order(detected, None) == detected


def test_apply_configured_fan_order_tolerates_drift() -> None:
    """A speed appearing later is appended; one that vanished is dropped."""
    assert apply_configured_fan_order(
        ["low", "med", "high", "turbo"], ["low", "med", "high", "retired"]
    ) == ["low", "med", "high", "turbo"]


@pytest.mark.asyncio
async def test_manager_reads_every_input_from_the_runtime() -> None:
    """The cycle must run off the runtime, not off scraped entity attributes."""
    runtime = _make_runtime()
    manager = await _build_manager(runtime)

    await manager.refresh_state()

    decision = manager.last_decision
    assert decision, "the cycle produced no decision"
    assert manager.fan_modes == FAN_MODES
    # 2 C above setpoint in cool: the MPC must not be idling.
    assert decision["mpc_status"] != "Idle"


@pytest.mark.asyncio
async def test_manager_applies_the_fan_mode_through_the_runtime() -> None:
    """A decided change is sent via async_set_underlying_fan_mode, not a service call."""
    runtime = _make_runtime()
    manager = await _build_manager(runtime)

    changed = await manager.refresh_state()

    if changed:
        runtime.async_set_underlying_fan_mode.assert_awaited_once()
        sent = runtime.async_set_underlying_fan_mode.await_args.args[0]
        assert sent in FAN_MODES
        assert manager._last_sent_fan_mode == sent  # noqa: SLF001


@pytest.mark.asyncio
async def test_manager_skips_the_cycle_on_incomplete_runtime_data() -> None:
    """VTherm can expose None during a restart; a skipped cycle beats a guess."""
    runtime = _make_runtime(current_temperature=None)
    manager = await _build_manager(runtime)

    assert await manager.refresh_state() is False
    runtime.async_set_underlying_fan_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_manager_detects_an_open_window_from_the_off_reason() -> None:
    """Window detection comes from VTherm's own off-reason, not attribute parsing."""
    runtime = _make_runtime(hvac_off_reason="hvac_off_window_detection")
    manager = await _build_manager(runtime)

    await manager.refresh_state()

    assert manager._is_window_open() is True  # noqa: SLF001
    assert manager.last_decision["mpc_status"] == "Disturbed"


@pytest.mark.asyncio
async def test_manager_treats_a_reported_idle_action_as_idle() -> None:
    """A device that says it is idle is believed."""
    manager = await _build_manager(hass=_make_hass(hvac_action="idle"))

    assert manager._is_hvac_idle() is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_manager_treats_an_off_underlying_as_idle() -> None:
    """An underlying that is off is off -- no hvac_action needed to know it."""
    manager = await _build_manager(
        hass=_make_hass(hvac_action=None, underlying_state="off")
    )

    assert manager._is_hvac_idle() is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_manager_is_not_idle_when_the_device_reports_no_action() -> None:
    """The equilibrium regression: no hvac_action must not read as idle.

    VTherm's is_device_active would say idle here, because it falls back to a
    target-vs-current sign check that is IDLE for the whole "at or past
    setpoint" region -- which is exactly where this controller does its work,
    with the unit still running. Trusting it froze fan control at equilibrium.
    """
    runtime = _make_runtime(is_device_active=False)
    manager = await _build_manager(runtime, hass=_make_hass(hvac_action=None))

    assert manager._is_hvac_idle() is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_manager_is_not_idle_while_the_device_is_producing() -> None:
    """A reported cooling action wins over VTherm's simulated verdict."""
    runtime = _make_runtime(is_device_active=False)
    manager = await _build_manager(runtime, hass=_make_hass(hvac_action="cooling"))

    assert manager._is_hvac_idle() is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_manager_detects_defrost_without_a_configured_entity() -> None:
    """A device reporting 'defrosting' needs no defrost entity configured.

    The configured entity is optional, so it cannot be the only defrost source.
    """
    manager = await _build_manager(hass=_make_hass(hvac_action="defrosting"))

    assert manager._is_defrost_active() is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_manager_adopts_fan_modes_published_after_startup() -> None:
    """An underlying that publishes its fan modes late must not stay unusable."""
    runtime = _make_runtime(underlying_fan_modes=None)
    manager = await _build_manager(runtime)
    assert manager.fan_modes is None

    runtime.underlying_fan_modes = list(FAN_MODES)
    await manager.refresh_state()

    assert manager.fan_modes == FAN_MODES


@pytest.mark.asyncio
async def test_manager_honours_the_configured_fan_order() -> None:
    """A ladder corrected in the options must reach the controller."""
    runtime = _make_runtime(underlying_fan_modes=["high", "low", "med"])
    manager = await _build_manager(
        runtime, fan_mode_order=["low", "med", "high"]
    )

    assert manager.fan_modes == ["low", "med", "high"]


@pytest.mark.asyncio
async def test_manager_adopts_the_vtherm_control_cadence() -> None:
    """The controller must know how often it is actually invoked."""
    runtime = _make_runtime(cycle_min=5)
    manager = await _build_manager(runtime)

    assert manager.mpc._cycle_minutes == 5  # noqa: SLF001


@pytest.mark.asyncio
async def test_prediction_grid_is_independent_of_the_control_cadence() -> None:
    """A slower VTherm cycle must not coarsen the predicted trajectory.

    These are separate concerns: how often we act is VTherm's to decide, how
    finely we integrate the forecast is a numerical choice. Sharing one constant
    let a 5-minute cycle drop the 30-minute horizon from 15 steps to 6, which
    changed which fan mode won on cost.
    """
    from custom_components.vtherm_mpc_fan.mpc_controller import SIMULATION_STEP_MINUTES

    predictions = []
    for cycle_min in (2, 5, 15):
        runtime = _make_runtime(cycle_min=cycle_min)
        manager = await _build_manager(runtime)
        decision = manager.mpc.evaluate(
            current_temp=24.0,
            target_temp=22.0,
            vtherm_slope=-0.8,
            hvac_mode="cool",
            current_fan="low",
            minutes_since_change=30.0,
        )
        predictions.append(
            (decision["mpc_fan_mode"], decision["mpc_predicted_temperature_30m"])
        )

    assert SIMULATION_STEP_MINUTES == 2
    assert len(set(predictions)) == 1, f"cadence changed the forecast: {predictions}"


@pytest.mark.asyncio
async def test_repeated_slope_readings_are_not_learned_twice() -> None:
    """A slope that has not moved is one measurement, not several.

    VTherm recomputes its slope on sensor events, so consecutive cycles often
    read the same number. Counting each as evidence would let a rarely-used
    speed clear the reliability gate without the observations to back it.
    """
    manager = await _build_manager()

    assert manager._is_duplicate_slope("low", "cool", -0.80) is False  # noqa: SLF001
    assert manager._is_duplicate_slope("low", "cool", -0.80) is True  # noqa: SLF001
    assert manager._is_duplicate_slope("low", "cool", -0.801) is True  # noqa: SLF001
    # A real move is accepted...
    assert manager._is_duplicate_slope("low", "cool", -0.95) is False  # noqa: SLF001
    # ...and the ladder is tracked per fan mode, so switching speed re-arms it.
    assert manager._is_duplicate_slope("high", "cool", -0.95) is False  # noqa: SLF001
    # ...as does switching hvac mode with the same reading.
    assert manager._is_duplicate_slope("high", "heat", -0.95) is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_forced_fan_mode_overrides_the_mpc() -> None:
    """The force_fan service pins a speed until its deadline passes."""
    import time

    runtime = _make_runtime()
    manager = await _build_manager(runtime)
    manager.force = FanOverride(fan_mode="silent", until=time.time() + 600)

    await manager.refresh_state()

    assert manager.last_decision["mpc_status"] == "Forced"
    assert manager.last_decision["mpc_fan_mode"] == "silent"


@pytest.mark.asyncio
async def test_expired_force_hands_control_back_to_the_mpc() -> None:
    """An override past its deadline is cleared rather than lingering."""
    import time

    runtime = _make_runtime()
    manager = await _build_manager(runtime)
    manager.force = FanOverride(fan_mode="silent", until=time.time() - 1)

    await manager.refresh_state()

    assert manager.force is None
    assert manager.last_decision["mpc_status"] != "Forced"


class TestShouldCollectSlopeSample:
    """Gating of slope-sample collection, ported from the standalone controller."""

    @staticmethod
    async def _manager(**over):
        manager = await _build_manager()
        manager._last_setpoint_drop_time = over.pop("last_setpoint_drop_time", 0.0)  # noqa: SLF001
        return manager

    @staticmethod
    def _kwargs(**over):
        base = dict(
            current_fan="low",
            hvac_mode="heat",
            is_defrost_active=False,
            is_hvac_idle=False,
            phase="ESTABLISHED",
            minutes_since_change=25.0,
            learned_dead_time=10.0,  # min stable window = 20 min
            now=1000.0,
        )
        base.update(over)
        return base

    @pytest.mark.asyncio
    async def test_happy_path_collects(self):
        manager = await self._manager()
        assert manager._should_collect_slope_sample(**self._kwargs()) is True  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_non_thermal_modes_are_skipped(self):
        """off/dry/fan_only produce no meaningful thermal response."""
        manager = await self._manager()
        for hvac_mode in ("off", "dry", "fan_only"):
            assert (
                manager._should_collect_slope_sample(**self._kwargs(hvac_mode=hvac_mode))  # noqa: SLF001
                is False
            )

    @pytest.mark.asyncio
    async def test_cool_mode_collects(self):
        manager = await self._manager()
        assert (
            manager._should_collect_slope_sample(**self._kwargs(hvac_mode="cool")) is True  # noqa: SLF001
        )

    @pytest.mark.asyncio
    async def test_no_fan_skipped(self):
        manager = await self._manager()
        assert (
            manager._should_collect_slope_sample(**self._kwargs(current_fan=None)) is False  # noqa: SLF001
        )

    @pytest.mark.asyncio
    async def test_defrost_or_idle_skipped(self):
        manager = await self._manager()
        assert (
            manager._should_collect_slope_sample(**self._kwargs(is_defrost_active=True))  # noqa: SLF001
            is False
        )
        assert (
            manager._should_collect_slope_sample(**self._kwargs(is_hvac_idle=True)) is False  # noqa: SLF001
        )

    @pytest.mark.asyncio
    async def test_non_established_phase_skipped(self):
        manager = await self._manager()
        assert (
            manager._should_collect_slope_sample(**self._kwargs(phase="DEAD_TIME")) is False  # noqa: SLF001
        )

    @pytest.mark.asyncio
    async def test_not_stable_long_enough_skipped(self):
        """12 min is below the 1.5 x 10 min dead-time settling window."""
        manager = await self._manager()
        assert (
            manager._should_collect_slope_sample(**self._kwargs(minutes_since_change=12.0))  # noqa: SLF001
            is False
        )

    @pytest.mark.asyncio
    async def test_recent_setpoint_drop_skipped(self):
        """A setpoint drop 1 min ago is still inside the learning cooldown."""
        manager = await self._manager(last_setpoint_drop_time=940.0)
        assert manager._should_collect_slope_sample(**self._kwargs(now=1000.0)) is False  # noqa: SLF001


class TestDefrostDetection:
    """Defrost is still read from a user-supplied entity: VTherm does not expose it."""

    @pytest.mark.asyncio
    async def test_defrost_entity_on_marks_active(self):
        hass = _make_hass()
        state = MagicMock()
        state.state = "on"
        hass.states.get = MagicMock(return_value=state)

        manager = await _build_manager(hass=hass, defrost_entity="binary_sensor.defrost")
        assert manager._is_defrost_active() is True  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_defrost_stays_active_through_the_cooldown(self):
        """The heat pump keeps recovering after the flag itself clears."""
        import time

        hass = _make_hass()
        state = MagicMock()
        state.state = "off"
        hass.states.get = MagicMock(return_value=state)

        manager = await _build_manager(hass=hass, defrost_entity="binary_sensor.defrost")
        manager._defrost_active = True  # noqa: SLF001
        manager._defrost_start_time = time.time()  # noqa: SLF001

        assert manager._is_defrost_active() is True  # noqa: SLF001

        # Push the start time past the cooldown window.
        manager._defrost_start_time = time.time() - 25 * 60  # noqa: SLF001
        assert manager._is_defrost_active() is False  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_no_defrost_entity_means_never_defrosting(self):
        manager = await _build_manager()
        assert manager._is_defrost_active() is False  # noqa: SLF001


def _auto_fan_entry(target_unique_id: str) -> MagicMock:
    """A vtherm_auto_fan_extended config entry aimed at the given VTherm."""
    entry = MagicMock()
    entry.data = {"target_vtherm_unique_id": target_unique_id}
    return entry


def _hass_with_auto_fan(target_unique_id: str | None):
    """A hass whose entry registry contains an auto-fan plugin for that VTherm."""
    hass = _make_hass()

    def async_entries(domain):
        if domain == "vtherm_auto_fan_extended" and target_unique_id:
            return [_auto_fan_entry(target_unique_id)]
        return []

    hass.config_entries.async_entries = MagicMock(side_effect=async_entries)
    return hass


class TestFanConflict:
    """Only one controller may own a fan; two of them make the speed flap."""

    @pytest.mark.asyncio
    async def test_no_command_is_sent_while_another_plugin_owns_the_fan(self):
        runtime = _make_runtime()
        manager = await _build_manager(runtime, hass=_hass_with_auto_fan("vtherm-uid"))

        changed = await manager.refresh_state()

        assert changed is False
        runtime.async_set_underlying_fan_mode.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_learning_and_diagnostics_keep_running_while_standing_down(self):
        """Yielding the actuator must not blind the model or the sensors."""
        runtime = _make_runtime()
        manager = await _build_manager(runtime, hass=_hass_with_auto_fan("vtherm-uid"))

        await manager.refresh_state()

        assert manager.last_decision, "the MPC stopped evaluating"
        attributes: dict = {}
        manager.add_custom_attributes(attributes)
        assert attributes["mpc_fan"]["conflicting_plugin"] == "vtherm_auto_fan_extended"

    @pytest.mark.asyncio
    async def test_a_plugin_on_another_vtherm_is_not_a_conflict(self):
        """The guard must key on the target thermostat, not on mere installation."""
        runtime = _make_runtime()
        manager = await _build_manager(runtime, hass=_hass_with_auto_fan("some-other-vtherm"))

        await manager.refresh_state()

        assert manager._conflict is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_control_resumes_once_the_conflict_is_removed(self):
        """The other plugin can be uninstalled later; the yield must reverse."""
        runtime = _make_runtime()
        hass = _make_hass()
        conflicting = {"on": True}

        def async_entries(domain):
            if domain == "vtherm_auto_fan_extended" and conflicting["on"]:
                return [_auto_fan_entry("vtherm-uid")]
            return []

        hass.config_entries.async_entries = MagicMock(side_effect=async_entries)
        manager = await _build_manager(runtime, hass=hass)

        await manager.refresh_state()
        runtime.async_set_underlying_fan_mode.assert_not_awaited()

        conflicting["on"] = False
        await manager.refresh_state()

        assert manager._conflict is None  # noqa: SLF001
        runtime.async_set_underlying_fan_mode.assert_awaited()


@pytest.mark.asyncio
async def test_manager_publishes_its_diagnostics_into_the_vtherm_state() -> None:
    """Diagnostics ride along in the VTherm's own attributes."""
    runtime = _make_runtime()
    manager = await _build_manager(runtime)
    await manager.refresh_state()

    attributes: dict = {}
    manager.add_custom_attributes(attributes)

    section = attributes["mpc_fan"]
    assert section["fan_mode_order"] == FAN_MODES
    assert "mpc_status" in section
    assert section["learning_ready"] is False



# --- Audit 2026-09: what counts as a fan change ----------------------------
@pytest.mark.asyncio
async def test_an_external_fan_change_restarts_the_dead_time() -> None:
    """A speed changed from the remote or an automation is still a change.

    Only the plugin's own commands used to reset the change clock, so a manual
    switch was immediately ESTABLISHED: its dead-time slopes were learned under
    the new speed and the MPC was free to override it on the next cycle.
    Another plugin owns the fan here so that no command of ours can be the
    cause of the change.
    """
    import time

    runtime = _make_runtime()
    hass = _hass_with_auto_fan("vtherm-uid")
    manager = await _build_manager(runtime, hass=hass)
    await manager.refresh_state()
    assert manager._last_change_time == 0.0  # noqa: SLF001

    hass.states.get(runtime.entity_id).attributes["fan_mode"] = "high"
    await manager.refresh_state()

    assert manager._last_change_time == pytest.approx(time.time(), abs=5)  # noqa: SLF001
    assert manager._response_armed is True  # noqa: SLF001
    assert manager.last_decision["minutes_since_last_change"] < 1.0


@pytest.mark.asyncio
async def test_only_the_first_slope_move_after_a_change_is_a_response_event() -> None:
    """One fan change, one response event.

    Every slope jump inside the 60-minute window used to be recorded as another
    response to the same change, so the median dead time drifted toward the
    middle of the window -- and everything gated on it (change interval,
    learning gate, simulated delay) stretched with it.
    """
    import time

    runtime = _make_runtime(last_temperature_slope=-0.2)
    manager = await _build_manager(runtime, hass=_hass_with_auto_fan("vtherm-uid"))
    await manager.refresh_state()
    manager._last_change_time = time.time() - 10 * 60  # noqa: SLF001
    manager._response_armed = True  # noqa: SLF001

    runtime.last_temperature_slope = -0.5
    await manager.refresh_state()
    assert manager.learning.response_event_count() == 1

    runtime.last_temperature_slope = -0.9
    await manager.refresh_state()
    assert manager.learning.response_event_count() == 1


@pytest.mark.asyncio
async def test_slope_samples_wait_for_the_learned_dead_time() -> None:
    """The learner runs on the same dead time as the controller.

    Gating the learner's dead time on is_ready() left it on the 10-minute
    default while the MPC worked with a measured 24 minutes: samples were taken
    20 minutes after a change, inside the real transient, labelled ESTABLISHED.
    """
    import time

    runtime = _make_runtime()
    manager = await _build_manager(runtime, hass=_hass_with_auto_fan("vtherm-uid"))
    for _ in range(20):
        manager.learning.add_response_event(24.0, "cool")

    manager._last_change_time = time.time() - 20 * 60  # noqa: SLF001
    await manager.refresh_state()
    assert manager.learning.slope_sample_count() == 0

    manager._last_change_time = time.time() - 40 * 60  # noqa: SLF001  (>= 1.5 x 24)
    await manager.refresh_state()
    assert manager.learning.slope_sample_count() == 1


FIXED_FAN_CONFIG = {"fixed_fan_hvac_modes": ["dry", "fan_only"], "fixed_fan_speed": "superhigh"}


@pytest.mark.asyncio
async def test_fixed_fan_is_applied_on_entering_a_fixed_mode() -> None:
    """In a fixed-speed mode the pinned speed is sent without waiting for the MPC."""
    runtime = _make_runtime(vtherm_hvac_mode="dry")
    manager = await _build_manager(runtime, **FIXED_FAN_CONFIG)

    changed = await manager.refresh_state()

    assert changed is True
    runtime.async_set_underlying_fan_mode.assert_awaited_once_with("superhigh")
    assert manager.last_decision["mpc_status"] == "Fixed"
    assert manager.last_decision["mpc_fan_mode"] == "superhigh"


@pytest.mark.asyncio
async def test_fixed_fan_does_not_apply_in_a_regulated_mode() -> None:
    """heat/cool stay with the MPC even when other modes are pinned."""
    runtime = _make_runtime(vtherm_hvac_mode="cool")
    manager = await _build_manager(runtime, **FIXED_FAN_CONFIG)

    await manager.refresh_state()

    assert manager.last_decision["mpc_status"] != "Fixed"


@pytest.mark.asyncio
async def test_unpinned_mode_without_regulation_leaves_the_fan_alone() -> None:
    """A mode that is neither regulated nor pinned holds the current fan."""
    runtime = _make_runtime(vtherm_hvac_mode="dry")
    manager = await _build_manager(runtime)

    changed = await manager.refresh_state()

    assert changed is False
    runtime.async_set_underlying_fan_mode.assert_not_awaited()
    assert manager.last_decision["mpc_status"] == "Idle"


@pytest.mark.asyncio
async def test_fixed_fan_respects_the_min_interval_after_a_change() -> None:
    """A manual change inside the mode is not reverted on the next cycle."""
    import time

    runtime = _make_runtime(vtherm_hvac_mode="dry")
    manager = await _build_manager(runtime, min_interval=10, **FIXED_FAN_CONFIG)
    await manager.refresh_state()
    runtime.async_set_underlying_fan_mode.reset_mock()

    await manager.refresh_state()  # same mode, change was just made
    runtime.async_set_underlying_fan_mode.assert_not_awaited()

    manager._last_change_time = time.time() - 11 * 60  # noqa: SLF001
    await manager.refresh_state()
    runtime.async_set_underlying_fan_mode.assert_awaited_once_with("superhigh")


@pytest.mark.asyncio
async def test_forced_fan_takes_precedence_over_the_fixed_fan() -> None:
    """force_fan is an explicit, time-boxed override and wins over the pin."""
    import time

    runtime = _make_runtime(vtherm_hvac_mode="dry")
    manager = await _build_manager(runtime, **FIXED_FAN_CONFIG)
    manager.force = FanOverride(fan_mode="silent", until=time.time() + 600)

    await manager.refresh_state()

    assert manager.last_decision["mpc_status"] == "Forced"
    runtime.async_set_underlying_fan_mode.assert_awaited_once_with("silent")


@pytest.mark.asyncio
async def test_fixed_fan_ignored_when_the_underlying_no_longer_offers_it() -> None:
    """A pinned speed that vanished degrades to holding the current fan."""
    runtime = _make_runtime(vtherm_hvac_mode="dry")
    manager = await _build_manager(
        runtime, fixed_fan_hvac_modes=["dry"], fixed_fan_speed="turbo"
    )

    changed = await manager.refresh_state()

    assert changed is False
    assert manager.last_decision["mpc_status"] == "Idle"
