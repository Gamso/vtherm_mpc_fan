"""Constants for VTherm MPC Fan."""
from datetime import timedelta

from homeassistant.util import slugify

DOMAIN = "vtherm_mpc_fan"
DEVICE_NAME = "Versatile Thermostat MPC Fan"
ENTITY_UNIQUE_ID_PREFIX = DOMAIN
EFFECTIVE_SLOPE_UNIT = "°C/h"
PROFILE_HVAC_MODES = ("heat", "cool")

CONF_TARGET_VTHERM = "target_vtherm"  # unique_id of the VTherm this plugin drives
CONF_DEADBAND = "deadband"
CONF_MIN_INTERVAL = "min_interval"
CONF_DATA_COLLECTION = "data_collection"
CONF_DEFROST_ENTITY = "defrost_entity"
CONF_FAN_MODE_ORDER = "fan_mode_order"  # explicit weakest-to-strongest order, overrides the climate entity's

# Feature-manager identity registered with the VTherm API.
FEATURE_MANAGER_MPC_FAN = "mpc_fan"

# VTherm integration domain and the thermostat type this plugin supports.
VTHERM_DOMAIN = "versatile_thermostat"
CONF_THERMOSTAT_TYPE = "thermostat_type"
CONF_THERMOSTAT_CLIMATE = "thermostat_over_climate"

# hass.data sub-keys (see registry.py)
DATA_FACTORY_REGISTERED = "factory_registered"
DATA_MANAGERS = "managers"
DATA_ADD_ENTITIES = "add_entities"
DATA_ENTITIES = "entities"

# VTherm reports this reason when it stopped the underlying because a window opened.
HVAC_OFF_REASON_WINDOW = "hvac_off_window_detection"

# Other known plugins that drive the same underlying fan_mode. Two controllers on
# one fan fight each other: each sees the other's command as an external change,
# so the speed flaps and both learn from a trajectory neither produced. They are
# listed with the config key naming their target VTherm, which differs per plugin.
CONFLICTING_FAN_PLUGINS = {
    "vtherm_auto_fan_extended": "target_vtherm_unique_id",
}

# Default values
DEFAULT_DEADBAND = 0.2
DEFAULT_MIN_INTERVAL = 10
DEFAULT_DATA_COLLECTION = True

# Fallback control-cycle length, used only until the runtime reports its own
# ``cycle_min``. It sets the MPC simulation step, so it should match the real
# cadence; VTherm's documentation recommends 5 minutes for over_climate.
DEFAULT_CYCLE_MINUTES = 5

# The control cycle is driven by VTherm: the feature manager's refresh_state() is
# called once per VTherm cycle (cycle_min), right after the regulated setpoint has
# been recomputed. This plugin deliberately keeps no timer of its own -- VTherm
# recomputes its temperature slope on sensor events, and measurements on a real
# 15-day trace showed 89% of consecutive 2-minute readings repeat the previous
# slope verbatim, so a faster private loop buys duplicates rather than evidence.
# See SLOPE_SAMPLE_MIN_DELTA for how those duplicates are kept out of learning.

# Storage
STORAGE_VERSION = 1
STORAGE_KEY = "vtherm_mpc_fan.learning_data"
LEARNING_DATA_SAVE_INTERVAL = timedelta(minutes=5)

# Controller thresholds
THRESHOLD_SLOPE = 0.1  # °C/h – minimum slope delta to trigger re-evaluation
THRESHOLD_TARGET_DROP = -1.0  # °C  – setpoint drop that triggers immediate speed cut
DEFAULT_DEAD_TIME = 10.0  # minutes – fallback dead time before learning is ready
DEAD_TIME_SAFETY_FACTOR = 1.5  # multiplier applied to learned dead time for effective timeout

# Phase detection
PHASE_DEAD_TIME = "DEAD_TIME"
PHASE_TRANSIENT = "TRANSIENT"
PHASE_ESTABLISHED = "ESTABLISHED"

# Learning
MIN_SAMPLES_LEARNING = 240  # Minimum slope samples required for initial readiness
MIN_MODE_PROFILE_SAMPLES = 10  # Minimum samples per fan mode to consider profile reliable
REFERENCE_SLOPE_ERROR = 1.0  # °C – reference comfort error at which the representative
# "working" effective slope is reported. The learned slope model is slope(error) = a + b·error;
# evaluating it at this gap yields a value reflecting real cooling/heating power rather than the
# near-equilibrium median, which is structurally diluted by samples taken close to the setpoint.
SETPOINT_DROP_LEARNING_COOLDOWN = 30.0  # Minutes to block learning after a setpoint drop
MIN_ESTABLISHED_RATIO = 2.0  # Minimum factor × dead_time the fan mode must be active before learning

# VTherm recomputes its temperature slope only when the room sensor reports a new
# value, so consecutive control cycles frequently observe the very same slope. A
# repeated reading is the same measurement seen twice, not new evidence, yet
# MIN_MODE_PROFILE_SAMPLES counts it as if it were independent -- which is how a
# rarely-used speed can appear to clear the reliability gate on a handful of real
# observations. Samples whose slope moved less than this since the last accepted
# one for that fan mode are therefore dropped.
SLOPE_SAMPLE_MIN_DELTA = 0.005  # °C/h


def build_unique_id(object_key: str, entry_id: str) -> str:
    """Build the canonical unique_id for an entity."""
    return f"{ENTITY_UNIQUE_ID_PREFIX}_{object_key}_{entry_id}"


def build_entity_id(platform_domain: str, object_key: str) -> str:
    """Build the canonical entity_id suggestion for an entity."""
    return f"{platform_domain}.{DOMAIN}_{object_key}"


def build_scoped_entity_id(platform_domain: str, climate_entity: str, object_key: str) -> str:
    """Build the climate-scoped entity_id suggestion for an entity."""
    climate_object_id = climate_entity.split(".", maxsplit=1)[-1]
    return f"{platform_domain}.{DOMAIN}_{slugify(climate_object_id)}_{object_key}"


def extract_object_key_from_unique_id(unique_id: str, entry_id: str) -> str | None:
    """Extract the object key from the canonical unique_id format."""
    prefix = f"{ENTITY_UNIQUE_ID_PREFIX}_"
    suffix = f"_{entry_id}"
    if not unique_id.startswith(prefix) or not unique_id.endswith(suffix):
        return None

    return unique_id[len(prefix) : -len(suffix)]
