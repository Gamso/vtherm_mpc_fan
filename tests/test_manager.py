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
    with patch("custom_components.vtherm_mpc_fan.manager.Store", return_value=_make_store()):
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
    assert apply_configured_fan_order(["low", "med", "high", "turbo"], ["low", "med", "high", "retired"]) == ["low", "med", "high", "turbo"]


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

    # 2 C above setpoint in cool on "low": the MPC must step up, so the command
    # is asserted unconditionally -- an `if changed:` guard let this pass vacuously.
    assert changed is True
    runtime.async_set_underlying_fan_mode.assert_awaited_once()
    sent = runtime.async_set_underlying_fan_mode.await_args.args[0]
    assert sent in FAN_MODES
    assert sent != "low"
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
    manager = await _build_manager(hass=_make_hass(hvac_action=None, underlying_state="off"))

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
    manager = await _build_manager(runtime, fan_mode_order=["low", "med", "high"])

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
        predictions.append((decision["mpc_fan_mode"], decision["mpc_predicted_temperature_30m"]))

    assert SIMULATION_STEP_MINUTES == 2
    assert len(set(predictions)) == 1, f"cadence changed the forecast: {predictions}"


@pytest.mark.asyncio
async def test_repeated_slope_readings_are_not_learned_twice() -> None:
    """A slope that has not moved is one measurement -- within one sampling interval.

    VTherm recomputes its slope on sensor events, so consecutive cycles often
    read the same number. Past SAMPLE_INTERVAL_MINUTES the same reading is a
    new sample: the regime held for another interval.
    """
    manager = await _build_manager()
    t0 = 1_000_000.0

    assert manager._is_duplicate_slope("low", "cool", -0.80, t0) is False  # noqa: SLF001
    manager._last_sample[("cool", "low")] = (-0.80, t0)  # noqa: SLF001
    assert manager._is_duplicate_slope("low", "cool", -0.80, t0 + 60) is True  # noqa: SLF001
    assert manager._is_duplicate_slope("low", "cool", -0.801, t0 + 300) is True  # noqa: SLF001
    # A real move is accepted...
    assert manager._is_duplicate_slope("low", "cool", -0.95, t0 + 300) is False  # noqa: SLF001
    # ...and the ladder is tracked per fan mode and per hvac mode.
    assert manager._is_duplicate_slope("high", "cool", -0.80, t0 + 60) is False  # noqa: SLF001
    assert manager._is_duplicate_slope("low", "heat", -0.80, t0 + 60) is False  # noqa: SLF001
    # One sampling interval later the unchanged reading is a new sample.
    assert manager._is_duplicate_slope("low", "cool", -0.80, t0 + 600) is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_held_regime_yields_one_sample_per_interval_with_its_dwell() -> None:
    """An unchanged reading over 20 minutes of established regime gives samples at 0, 10, 20 min."""
    runtime = _make_runtime(last_temperature_slope=-0.3)
    manager = await _build_manager(runtime, hass=_hass_with_auto_fan("vtherm-uid"))
    t0 = 2_000_000.0

    for minute in range(0, 25, 5):
        with patch("time.time", return_value=t0 + minute * 60):
            await manager.refresh_state()

    samples = manager.learning.slope_samples
    assert len(samples) == 3
    assert [s[6] for s in samples] == [0.0, 10.0, 10.0]


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
            assert manager._should_collect_slope_sample(**self._kwargs(hvac_mode=hvac_mode)) is False  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_cool_mode_collects(self):
        manager = await self._manager()
        assert manager._should_collect_slope_sample(**self._kwargs(hvac_mode="cool")) is True  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_no_fan_skipped(self):
        manager = await self._manager()
        assert manager._should_collect_slope_sample(**self._kwargs(current_fan=None)) is False  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_defrost_or_idle_skipped(self):
        manager = await self._manager()
        assert manager._should_collect_slope_sample(**self._kwargs(is_defrost_active=True)) is False  # noqa: SLF001
        assert manager._should_collect_slope_sample(**self._kwargs(is_hvac_idle=True)) is False  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_non_established_phase_skipped(self):
        manager = await self._manager()
        assert manager._should_collect_slope_sample(**self._kwargs(phase="DEAD_TIME")) is False  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_not_stable_long_enough_skipped(self):
        """12 min is below the 1.5 x 10 min dead-time settling window."""
        manager = await self._manager()
        assert manager._should_collect_slope_sample(**self._kwargs(minutes_since_change=12.0)) is False  # noqa: SLF001

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


class TestNativeAutoFanConflict:
    """VTherm's built-in auto-fan sends a fan command every cycle: it is a competing controller."""

    @pytest.mark.asyncio
    async def test_an_enabled_native_auto_fan_makes_the_manager_stand_down(self):
        """With auto_fan_mode set, no command is sent and the conflict is exposed."""
        runtime = _make_runtime(entry_infos={"thermostat_type": "thermostat_over_climate", "auto_fan_mode": "auto_fan_high"})
        manager = await _build_manager(runtime)

        changed = await manager.refresh_state()

        assert changed is False
        runtime.async_set_underlying_fan_mode.assert_not_awaited()
        attributes: dict = {}
        manager.add_custom_attributes(attributes)
        assert attributes["mpc_fan"]["conflicting_plugin"] == "versatile_thermostat/auto_fan_mode"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["auto_fan_none", None])
    async def test_a_disabled_native_auto_fan_is_not_a_conflict(self, mode):
        """auto_fan_none (or no value) leaves the fan to this plugin."""
        runtime = _make_runtime(entry_infos={"thermostat_type": "thermostat_over_climate", "auto_fan_mode": mode})
        manager = await _build_manager(runtime)

        await manager.refresh_state()

        assert manager._conflict is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_control_resumes_once_the_native_auto_fan_is_disabled(self):
        """Switching the VTherm option to None hands the fan back on the next cycle."""
        infos = {"thermostat_type": "thermostat_over_climate", "auto_fan_mode": "auto_fan_turbo"}
        runtime = _make_runtime(entry_infos=infos)
        manager = await _build_manager(runtime)
        await manager.refresh_state()
        runtime.async_set_underlying_fan_mode.assert_not_awaited()

        infos["auto_fan_mode"] = "auto_fan_none"
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
async def test_the_dead_time_is_the_first_sensor_step_in_the_expected_direction() -> None:
    """One fan change, one response event, measured on the temperature.

    A climb in cool must cool the room: a reading moving the wrong way is not
    the response, the first step of one sensor resolution the right way is, and
    nothing after it counts again. Detecting it on the slope EMA fired on the
    next reading whatever its direction -- the "dead time" was the wait for it.
    """
    import time

    runtime = _make_runtime(current_temperature=24.6)
    manager = await _build_manager(runtime, hass=_hass_with_auto_fan("vtherm-uid"))
    await manager.refresh_state()
    manager._register_fan_change(time.time() - 10 * 60, 24.6, +1)  # noqa: SLF001  (low -> stronger)

    runtime.current_temperature = 24.8  # wrong way
    await manager.refresh_state()
    assert manager.learning.response_event_count() == 0

    runtime.current_temperature = 24.4  # one 0.2 step cooler than at the change
    await manager.refresh_state()
    assert manager.learning.response_event_count() == 1
    assert manager.learning.get_dead_time("cool") == pytest.approx(10.0, abs=0.5)

    runtime.current_temperature = 24.0
    await manager.refresh_state()
    assert manager.learning.response_event_count() == 1


@pytest.mark.asyncio
async def test_a_step_down_expects_the_room_to_move_the_other_way() -> None:
    """After a weaker speed in cool the response is the room warming."""
    import time

    runtime = _make_runtime(current_temperature=24.0)
    manager = await _build_manager(runtime, hass=_hass_with_auto_fan("vtherm-uid"))
    await manager.refresh_state()
    manager._register_fan_change(time.time() - 12 * 60, 24.0, -1)  # noqa: SLF001

    runtime.current_temperature = 23.8
    await manager.refresh_state()
    assert manager.learning.response_event_count() == 0
    runtime.current_temperature = 24.2
    await manager.refresh_state()
    assert manager.learning.response_event_count() == 1


@pytest.mark.asyncio
async def test_the_change_direction_follows_the_ladder() -> None:
    """Stronger is +1, weaker -1, unknown speeds 0 (no event is armed)."""
    manager = await _build_manager()
    manager._sync_fan_modes()  # noqa: SLF001

    assert manager._change_direction("low", "high") == 1  # noqa: SLF001
    assert manager._change_direction("superhigh", "silent") == -1  # noqa: SLF001
    assert manager._change_direction("low", "turbo") == 0  # noqa: SLF001
    manager._register_fan_change(1.0, 24.0, 0)  # noqa: SLF001
    assert manager._response_armed is False  # noqa: SLF001


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


async def _enter_fixed_mode(runtime, manager, hvac_mode: str = "dry") -> bool:
    """Run one regulated cycle, switch the VTherm to *hvac_mode*, run one more.

    Entering a mode is a transition between two observed cycles: a manager's
    very first cycle has no previous mode to have left. The command the cool
    cycle may have sent is cleared so assertions only see the mode entry.
    Returns what the entry cycle returned.
    """
    runtime.vtherm_hvac_mode = "cool"
    await manager.refresh_state()
    runtime.async_set_underlying_fan_mode.reset_mock()
    manager._last_change_time = 0.0  # noqa: SLF001  (forget the cool cycle's change)
    runtime.vtherm_hvac_mode = hvac_mode
    return await manager.refresh_state()


@pytest.mark.asyncio
async def test_fixed_fan_is_applied_on_entering_a_fixed_mode() -> None:
    """In a fixed-speed mode the pinned speed is sent without waiting for the MPC."""
    runtime = _make_runtime()
    manager = await _build_manager(runtime, min_interval=10, **FIXED_FAN_CONFIG)

    changed = await _enter_fixed_mode(runtime, manager)

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

    runtime = _make_runtime()
    manager = await _build_manager(runtime, min_interval=10, **FIXED_FAN_CONFIG)
    await _enter_fixed_mode(runtime, manager)
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
    manager = await _build_manager(runtime, fixed_fan_hvac_modes=["dry"], fixed_fan_speed="turbo")

    changed = await manager.refresh_state()

    assert changed is False
    assert manager.last_decision["mpc_status"] == "Idle"


@pytest.mark.asyncio
async def test_fixed_fan_is_not_forced_on_the_first_cycle_after_a_restart() -> None:
    """A restart or reload must not overwrite a speed the user set by hand.

    A fresh manager's first cycle used to count as "entering" the mode (its
    previous mode was None), so every restart, VTherm reload or options change
    sent the pin at once -- reverting a manual speed the contract says is kept
    until the min interval has elapsed.
    """
    runtime = _make_runtime(vtherm_hvac_mode="dry")
    manager = await _build_manager(runtime, min_interval=10, **FIXED_FAN_CONFIG)

    changed = await manager.refresh_state()

    assert changed is False
    runtime.async_set_underlying_fan_mode.assert_not_awaited()
    assert manager.last_decision["mpc_status"] == "Fixed"
    assert manager.last_decision["mpc_would_change_now"] == "no"


@pytest.mark.asyncio
async def test_fixed_fan_after_a_restart_waits_the_min_interval_from_the_first_cycle() -> None:
    """Without a change history the pin's clock starts at the manager's first cycle."""
    runtime = _make_runtime(vtherm_hvac_mode="dry")
    manager = await _build_manager(runtime, min_interval=10, **FIXED_FAN_CONFIG)
    await manager.refresh_state()

    manager._first_cycle_time -= 9 * 60  # noqa: SLF001
    await manager.refresh_state()
    runtime.async_set_underlying_fan_mode.assert_not_awaited()

    manager._first_cycle_time -= 2 * 60  # noqa: SLF001  (11 min since the first cycle)
    await manager.refresh_state()
    runtime.async_set_underlying_fan_mode.assert_awaited_once_with("superhigh")


@pytest.mark.asyncio
async def test_pinned_speed_commands_generate_no_response_events() -> None:
    """The pin's own commands must not teach the model a dead time.

    Every command arms the response detector; in dry the room still moves (a
    dehumidifier changes it), so each pinned command used to record a
    "dry" response event. Five of them made the adaptive interval trust a dead
    time learned outside heat/cool.
    """
    import time

    runtime = _make_runtime(last_temperature_slope=-0.2)
    hass = _make_hass()
    manager = await _build_manager(runtime, hass=hass, min_interval=10, **FIXED_FAN_CONFIG)
    await _enter_fixed_mode(runtime, manager)
    runtime.async_set_underlying_fan_mode.assert_awaited_once_with("superhigh")
    # The climate now reports the pinned speed, so the pin holds rather than resends.
    hass.states.get(runtime.entity_id).attributes["fan_mode"] = "superhigh"
    await manager.refresh_state()

    for temp in (23.4, 24.2, 23.0, 24.4, 22.8):
        manager._register_fan_change(time.time() - 10 * 60, runtime.current_temperature, +1)  # noqa: SLF001  (as after any command)
        runtime.current_temperature = temp
        await manager.refresh_state()

    assert manager.learning.response_event_count() == 0


@pytest.mark.asyncio
async def test_a_pending_response_does_not_cross_an_hvac_mode_change() -> None:
    """A fan change made in dry is not the cause of a temperature move seen in cool."""
    import time

    runtime = _make_runtime(vtherm_hvac_mode="dry", current_temperature=25.0)
    manager = await _build_manager(runtime, hass=_hass_with_auto_fan("vtherm-uid"))
    await manager.refresh_state()
    manager._register_fan_change(time.time() - 10 * 60, 25.0, +1)  # noqa: SLF001

    runtime.vtherm_hvac_mode = "cool"
    await manager.refresh_state()
    runtime.current_temperature = 24.4
    await manager.refresh_state()

    assert manager.learning.response_event_count() == 0


async def _pinned_manager_with_a_manual_speed(runtime, hass):
    """Enter dry with a pin, then let the min interval lapse after a manual speed change.

    Leaves the manager one cycle away from re-applying the pin, so a test only
    has to add the disturbance and check that nothing is sent.
    """
    import time

    manager = await _build_manager(runtime, hass=hass, min_interval=10, **FIXED_FAN_CONFIG)
    await _enter_fixed_mode(runtime, manager)
    runtime.async_set_underlying_fan_mode.reset_mock()
    manager._last_change_time = time.time() - 11 * 60  # noqa: SLF001
    return manager


@pytest.mark.asyncio
async def test_fixed_fan_is_held_while_a_window_is_open() -> None:
    """No pinned command reaches a unit VTherm stopped for an open window."""
    runtime = _make_runtime()
    manager = await _pinned_manager_with_a_manual_speed(runtime, _make_hass())
    runtime.hvac_off_reason = "hvac_off_window_detection"

    assert await manager.refresh_state() is False
    runtime.async_set_underlying_fan_mode.assert_not_awaited()
    assert manager.last_decision["mpc_would_change_now"] == "no"
    assert "window open" in manager.last_decision["mpc_reason"]

    runtime.hvac_off_reason = None
    await manager.refresh_state()
    runtime.async_set_underlying_fan_mode.assert_awaited_once_with("superhigh")


@pytest.mark.asyncio
async def test_fixed_fan_is_held_while_the_underlying_is_off() -> None:
    """Some IR/cloud climates read set_fan_mode as power-on: an off unit is left alone."""
    runtime = _make_runtime()
    hass = _make_hass()
    manager = await _pinned_manager_with_a_manual_speed(runtime, hass)
    hass.states.get(UNDERLYING_ENTITY).state = "off"

    assert await manager.refresh_state() is False
    runtime.async_set_underlying_fan_mode.assert_not_awaited()
    assert "underlying off" in manager.last_decision["mpc_reason"]


@pytest.mark.asyncio
async def test_fixed_fan_is_held_during_defrost() -> None:
    """A defrosting heat pump gets no fan command, pinned or not."""
    runtime = _make_runtime()
    hass = _make_hass()
    manager = await _pinned_manager_with_a_manual_speed(runtime, hass)
    hass.states.get(UNDERLYING_ENTITY).attributes["hvac_action"] = "defrosting"

    assert await manager.refresh_state() is False
    runtime.async_set_underlying_fan_mode.assert_not_awaited()
    assert "defrost active" in manager.last_decision["mpc_reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("hvac_mode", ["dry", "fan_only"])
async def test_unregulated_modes_never_start_the_setpoint_drop_cooldown(hvac_mode: str) -> None:
    """A warm room in dry is not a setpoint drop.

    The error was computed with the heating convention in every non-cool
    mode: 27 C for a 24 C setpoint read as -3 C, a "setpoint drop" on every
    dry cycle, and slope learning stayed blocked for 30 min after switching
    to cool although no setpoint had moved.
    """
    runtime = _make_runtime(vtherm_hvac_mode=hvac_mode, current_temperature=27.0, regulated_target_temperature=24.0)
    manager = await _build_manager(runtime)

    await manager.refresh_state()

    assert manager._last_setpoint_drop_time == 0.0  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_real_setpoint_drop_in_heat_still_starts_the_cooldown() -> None:
    """The user lowering the heating setpoint by 2 degC starts the learning cooldown."""
    runtime = _make_runtime(vtherm_hvac_mode="heat", current_temperature=24.0, target_temperature=24.0, regulated_target_temperature=24.0)
    manager = await _build_manager(runtime)
    await manager.refresh_state()
    assert manager._last_setpoint_drop_time == 0.0  # noqa: SLF001

    runtime.target_temperature = 22.0
    runtime.regulated_target_temperature = 22.0
    await manager.refresh_state()

    assert manager.last_decision["mpc_status"] == "Setpoint drop"
    assert manager._last_setpoint_drop_time > 0.0  # noqa: SLF001


@pytest.mark.asyncio
async def test_an_overshoot_without_a_setpoint_change_starts_no_cooldown() -> None:
    """A room 1.5 degC past an unchanged setpoint is learnable: no 30-minute cooldown."""
    runtime = _make_runtime(vtherm_hvac_mode="cool", current_temperature=22.5, target_temperature=24.0, regulated_target_temperature=23.2)
    manager = await _build_manager(runtime)

    await manager.refresh_state()
    await manager.refresh_state()

    assert manager.last_decision["mpc_status"] == "Overshoot"
    assert manager._last_setpoint_drop_time == 0.0  # noqa: SLF001


@pytest.mark.asyncio
async def test_learning_samples_carry_the_comfort_error_and_the_offset() -> None:
    """Samples are taken against the user's setpoint and keep the regulation offset."""
    runtime = _make_runtime(vtherm_hvac_mode="cool", current_temperature=24.3, target_temperature=24.0, regulated_target_temperature=23.4)
    manager = await _build_manager(runtime, hass=_hass_with_auto_fan("vtherm-uid"))

    await manager.refresh_state()

    sample = manager.learning.slope_samples[-1]
    assert sample[4] == pytest.approx(0.3)
    assert sample[5] == pytest.approx(-0.6)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "detected"),
    [
        ("Ready", True),
        ("Low confidence", True),
        ("Setpoint drop", True),
        ("Forced", False),
        ("Fixed", False),
        ("Unavailable", False),
        ("Idle", False),
        ("Disturbed", False),
        (None, False),
    ],
)
async def test_is_detected_means_the_mpc_is_steering(status, detected) -> None:
    """Forced and Fixed set the fan, but they are not MPC regulation."""
    manager = await _build_manager()
    manager._last_decision = {"mpc_status": status} if status else {}  # noqa: SLF001

    assert manager.is_detected is detected


@pytest.mark.asyncio
async def test_is_detected_is_false_in_a_pinned_mode() -> None:
    """End to end: a cycle in a fixed-speed mode does not report regulation."""
    runtime = _make_runtime(vtherm_hvac_mode="dry")
    manager = await _build_manager(runtime, **FIXED_FAN_CONFIG)

    await manager.refresh_state()

    assert manager.last_decision["mpc_status"] == "Fixed"
    assert manager.is_detected is False


@pytest.mark.asyncio
async def test_the_csv_row_carries_the_user_setpoint_and_the_regulation_offset() -> None:
    """VTherm regulates the user's setpoint: both, their offset and the comfort error are logged."""
    runtime = _make_runtime(current_temperature=24.0, target_temperature=24.0, regulated_target_temperature=23.4)
    manager = await _build_manager(runtime)
    manager._collector = MagicMock(async_record=AsyncMock())  # noqa: SLF001

    await manager.refresh_state()

    kwargs = manager._collector.async_record.await_args.kwargs  # noqa: SLF001
    assert kwargs["target_temp"] == pytest.approx(23.4)
    assert kwargs["user_target_temp"] == pytest.approx(24.0)
    assert kwargs["regulation_offset"] == pytest.approx(-0.6)
    assert kwargs["comfort_error"] == pytest.approx(0.0)
