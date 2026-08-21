"""Sensor platform for VTherm MPC Fan."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_TARGET_VTHERM,
    DEVICE_NAME,
    DOMAIN,
    EFFECTIVE_SLOPE_UNIT,
    build_scoped_entity_id,
    build_unique_id,
)
from .registry import add_entities_registry, entity_bucket, get_manager

PLATFORM_SENSOR = "sensor"

_LOGGER = logging.getLogger(__name__)


class _SmartFanEntity(SensorEntity):
    """Base sensor wired to the VTherm MPC Fan device.

    Every sensor derives its identity the same way -- unique_id and the
    climate-scoped entity_id both come from one object key -- so that is done
    here rather than repeated in each subclass.
    """

    _attr_has_entity_name = True

    def __init__(self, entry_id: str, climate_entity: str, object_key: str) -> None:
        super().__init__()
        self._entry_id = entry_id
        self._climate_entity = climate_entity
        self._attr_unique_id = build_unique_id(object_key, entry_id)
        # Set here rather than by the subclasses so entity_id always exists
        # before Home Assistant adds the entity.
        self.entity_id = build_scoped_entity_id("sensor", climate_entity, object_key)

    @property
    def device_info(self) -> DeviceInfo:
        """Link the entity to the VTherm MPC Fan device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=DEVICE_NAME,
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Publish the platform callback and build the sensors if the manager is up.

    Home Assistant sets this platform up when the plugin's config entry loads,
    while VTherm creates the feature manager on its own schedule -- neither order
    is guaranteed. The callback is therefore parked in the registry and whichever
    side arrives second does the work: here when the manager already exists, or
    from ``MpcFanFeatureManager.ensure_entities()`` on its first cycle.
    """
    target_unique_id = entry.data.get(CONF_TARGET_VTHERM)
    if not target_unique_id:
        _LOGGER.error("Config entry %s has no target VTherm; no sensor created", entry.entry_id)
        return

    # Merged into the registry entry rather than replacing it outright: the
    # number platform's own async_setup_entry writes to the same dict, on its
    # own schedule, and a plain assignment here would erase its callback if it
    # happened to run first.
    registry = add_entities_registry(hass).setdefault(target_unique_id, {})
    registry[PLATFORM_SENSOR] = async_add_entities
    registry.setdefault("entry_id", entry.entry_id)

    manager = get_manager(hass, target_unique_id)
    if manager is not None:
        manager.ensure_entities()


def build_entities(manager) -> list[SensorEntity]:
    """Create every sensor for a manager whose fan ladder is known.

    Returns only the entities that do not exist yet, so this is safe to call on
    each cycle: the fan-mode ladder can grow after startup, and the per-profile
    slope sensors must follow it.
    """
    hass = manager.hass
    bucket = entity_bucket(hass, manager.vtherm_unique_id)
    registry = add_entities_registry(hass).get(manager.vtherm_unique_id) or {}
    entry_id = registry.get("entry_id", manager.vtherm_unique_id)
    climate_entity = manager.vtherm.entity_id
    mpc = manager.mpc
    if mpc is None:
        return []

    new_entities: list[SensorEntity] = []

    sensor_definitions = [
        # No ENUM device_class: it requires a static `options` list, but fan modes
        # are discovered at runtime and vary per climate, which would emit HA
        # validation warnings. A plain text sensor shows the fan mode just fine.
        ("Fan Mode", "fan_mode", "fan_mode", None, None, "mdi:fan", None),
        (
            "Fan Mode Last Change",
            "fan_mode_last_change",
            "minutes_since_last_change",
            UnitOfTime.MINUTES,
            SensorDeviceClass.DURATION,
            "mdi:clock-outline",
            EntityCategory.DIAGNOSTIC,
        ),
        ("MPC Confidence", "mpc_confidence", "mpc_confidence", PERCENTAGE, None, "mdi:chart-line", EntityCategory.DIAGNOSTIC),
        (
            "MPC Predicted Temperature 10 Min",
            "mpc_predicted_temperature_10_min",
            "mpc_predicted_temperature_10m",
            UnitOfTemperature.CELSIUS,
            SensorDeviceClass.TEMPERATURE,
            "mdi:chart-timeline-variant",
            EntityCategory.DIAGNOSTIC,
        ),
        (
            "MPC Predicted Temperature 30 Min",
            "mpc_predicted_temperature_30_min",
            "mpc_predicted_temperature_30m",
            UnitOfTemperature.CELSIUS,
            SensorDeviceClass.TEMPERATURE,
            "mdi:chart-timeline-variant",
            EntityCategory.DIAGNOSTIC,
        ),
        ("MPC Disturbance Bias", "mpc_disturbance_bias", "mpc_disturbance_bias", EFFECTIVE_SLOPE_UNIT, None, "mdi:weather-windy", EntityCategory.DIAGNOSTIC),
    ]
    # mpc_status, mpc_reason, mpc_fan_mode, mpc_would_change_now, mpc_cost and
    # mpc_known_profiles were dropped as standalone entities: point-in-time
    # values with no history-graph or automation use that VTherm's own
    # extra_state_attributes.mpc_fan already exposes (see
    # MpcFanFeatureManager.add_custom_attributes), and mpc_controller.evaluate()
    # logs every one of them at DEBUG on every cycle via ``_payload()``.
    #
    # mpc_dead_time was also dropped: it is the same learning.get_dead_time()
    # value as the "Learned Dead Time" sensor below, just read through the last
    # decision payload instead of directly -- two entities for one number.

    if not bucket["sensors"]:
        for name, object_key, controller_key, unit, device_class, icon, entity_category in sensor_definitions:
            entity = SmartFanSensor(
                entry_id,
                climate_entity,
                name,
                object_key,
                controller_key,
                unit,
                device_class,
                icon,
                entity_category,
            )
            bucket["sensors"][object_key] = entity
            new_entities.append(entity)

        for key, entity in (
            ("learning", SmartFanLearningSensor(entry_id, climate_entity, mpc)),
            ("learning_samples", SmartFanLearningSamplesSensor(entry_id, climate_entity, mpc)),
            ("learning_response", SmartFanLearningResponseSensor(entry_id, climate_entity, mpc)),
            ("learned_dead_time", SmartFanLearnedDeadTimeSensor(entry_id, climate_entity, mpc)),
            ("effective_timeout", SmartFanEffectiveTimeoutSensor(entry_id, climate_entity, mpc)),
            ("learned_deadband", SmartFanLearnedDeadbandSensor(entry_id, climate_entity, mpc)),
        ):
            bucket["sensors"][key] = entity
            new_entities.append(entity)

    return new_entities


class SmartFanSensor(_SmartFanEntity):
    """A specific sensor fed by the live controller payload."""

    def __init__(
        self,
        entry_id: str,
        climate_entity: str,
        name_suffix: str,
        object_key: str,
        data_key: str,
        unit: str | None,
        device_class: SensorDeviceClass | None,
        icon: str,
        entity_category: EntityCategory | None = EntityCategory.DIAGNOSTIC,
    ) -> None:
        super().__init__(entry_id, climate_entity, object_key)
        self._data_key = data_key
        self._attr_name = name_suffix
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_native_value = None
        self._attr_icon = icon
        self._attr_entity_category = entity_category

    def update_from_mpc(self, data: dict) -> None:
        """Update the sensor value with data from the controller."""
        if self._data_key in data:
            self._attr_native_value = data.get(self._data_key)


class SmartFanLearningSensor(_SmartFanEntity):
    """Sensor showing learning progress and optimal parameters."""

    def __init__(self, entry_id: str, climate_entity: str, controller) -> None:
        super().__init__(entry_id, climate_entity, "learning_progress")
        self._controller = controller
        self._attr_name = "Learning Progress"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_icon = "mdi:school"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> float:
        """Return learning progress percentage."""
        return round(self._controller.learning.get_progress(), 1)

    @property
    def extra_state_attributes(self) -> dict:
        """Return optimal parameters continuously, even before ready."""
        attrs = {
            "samples_collected": self._controller.learning.slope_sample_count(),
            "response_events": self._controller.learning.response_event_count(),
            "is_ready": self._controller.learning.is_ready(),
            "learned_dead_time": round(self._controller.learning.get_dead_time(), 2),
            "effective_timeout": round(self._controller.get_effective_timeout(), 2),
        }

        optimal = self._controller.learning.compute_optimal_parameters()
        if optimal:
            attrs["learned_deadband"] = optimal.get("deadband")
            attrs["learned_samples_count"] = optimal.get("samples_count")
            attrs["learned_response_samples"] = optimal.get("response_samples")

        return attrs


class SmartFanLearningSamplesSensor(_SmartFanEntity):
    """Sensor showing number of slope samples collected."""

    def __init__(self, entry_id: str, climate_entity: str, controller) -> None:
        super().__init__(entry_id, climate_entity, "learning_samples")
        self._controller = controller
        self._attr_name = "Learning Samples"
        self._attr_native_unit_of_measurement = "samples"
        self._attr_icon = "mdi:chart-box-outline"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> int:
        """Return number of samples collected."""
        return self._controller.learning.slope_sample_count()

    @property
    def extra_state_attributes(self) -> dict:
        """Return sample statistics."""
        learning = self._controller.learning
        optimal = learning.compute_optimal_parameters()

        return {
            "min_samples_required": learning.min_samples,
            "slope_mean": round(learning.slope_mean, 3),
            "slope_stdev": round(((learning.slope_m2 / (learning.slope_count - 1)) ** 0.5) if learning.slope_count > 1 else 0, 3),
            "slope_max": round(learning.slope_max, 3),
            "samples_count": optimal.get("samples_count", 0),
        }


class SmartFanLearningResponseSensor(_SmartFanEntity):
    """Sensor showing number of response events recorded."""

    def __init__(self, entry_id: str, climate_entity: str, controller) -> None:
        super().__init__(entry_id, climate_entity, "learning_response_events")
        self._controller = controller
        self._attr_name = "Learning Response Events"
        self._attr_native_unit_of_measurement = "events"
        self._attr_icon = "mdi:timer-outline"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> int:
        """Return number of response events."""
        return self._controller.learning.response_event_count()

    @property
    def extra_state_attributes(self) -> dict:
        """Return response time statistics."""
        learning = self._controller.learning
        optimal = learning.compute_optimal_parameters()
        response_times = [item[1] for item in learning.response_events if item[1] > 0]
        avg_response = sum(response_times) / len(response_times) if response_times else 0

        return {
            "response_samples": optimal.get("response_samples", 0),
            "avg_response_time_min": round(avg_response, 1),
            "median_response_time_min": round(learning.get_dead_time(), 2),
            "effective_timeout_min": round(self._controller.get_effective_timeout(), 2),
        }


class SmartFanLearnedDeadTimeSensor(_SmartFanEntity):
    """Sensor showing the learned thermal dead time."""

    def __init__(self, entry_id: str, climate_entity: str, controller) -> None:
        super().__init__(entry_id, climate_entity, "learned_dead_time")
        self._controller = controller
        self._attr_name = "Learned Dead Time"
        self._attr_native_unit_of_measurement = UnitOfTime.MINUTES
        self._attr_device_class = SensorDeviceClass.DURATION
        self._attr_icon = "mdi:timer-sand"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> float:
        """Return the median learned response delay."""
        return round(self._controller.learning.get_dead_time(), 2)

    @property
    def extra_state_attributes(self) -> dict:
        """Expose readiness context for the learned dead time."""
        return {
            "is_ready": self._controller.learning.is_ready(),
            "response_events": self._controller.learning.response_event_count(),
        }


class SmartFanEffectiveTimeoutSensor(_SmartFanEntity):
    """Sensor showing the actual non-emergency timeout in use."""

    def __init__(self, entry_id: str, climate_entity: str, controller) -> None:
        super().__init__(entry_id, climate_entity, "effective_timeout")
        self._controller = controller
        self._attr_name = "Effective Timeout"
        self._attr_native_unit_of_measurement = UnitOfTime.MINUTES
        self._attr_device_class = SensorDeviceClass.DURATION
        self._attr_icon = "mdi:clock-check-outline"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> float:
        """Return the actual timeout currently used by controller decisions."""
        return round(self._controller.get_effective_timeout(), 2)

    @property
    def extra_state_attributes(self) -> dict:
        """Show how the effective timeout is derived."""
        return {
            "is_ready": self._controller.learning.is_ready(),
            "learned_dead_time": round(self._controller.learning.get_dead_time(), 2),
        }


class _BaseLearnedParameterSensor(_SmartFanEntity):
    """Base class for learned parameter sensors."""

    def __init__(
        self,
        entry_id: str,
        climate_entity: str,
        controller,
        *,
        name: str,
        object_key: str,
        unit,
        device_class,
        learning_key: str,
        icon: str = "mdi:brain",
        current_attr: str | None = None,
    ) -> None:
        super().__init__(entry_id, climate_entity, object_key)
        self._controller = controller
        self._learning_key = learning_key
        self._current_attr = current_attr
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_icon = icon
        self._attr_native_value = None
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        """Return the learned value, or current value if not ready yet."""
        optimal = self._controller.learning.compute_optimal_parameters()
        if optimal:
            value = optimal.get(self._learning_key)
            return round(value, 2) if value is not None else None

        if self._current_attr:
            value = getattr(self._controller, f"_{self._current_attr}", 0)
            return round(value, 2) if value else 0

        return 0

    @property
    def extra_state_attributes(self) -> dict:
        """Expose readiness and sample counts for context."""
        learning = self._controller.learning
        return {
            "is_ready": learning.is_ready(),
            "samples_collected": learning.slope_sample_count(),
            "response_events": learning.response_event_count(),
        }


class SmartFanLearnedDeadbandSensor(_BaseLearnedParameterSensor):
    """Learned deadband parameter."""

    def __init__(self, entry_id: str, climate_entity: str, controller) -> None:
        super().__init__(
            entry_id,
            climate_entity,
            controller,
            name="Learned Deadband",
            object_key="learned_deadband",
            unit=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            learning_key="deadband",
            icon="mdi:thermometer-lines",
            current_attr="deadband",
        )
