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
CONF_FIXED_FAN_HVAC_MODES = "fixed_fan_hvac_modes"  # HVAC modes (other than heat/cool) in which the fan is pinned to a fixed speed
CONF_FIXED_FAN_SPEED = "fixed_fan_speed"  # the speed pinned in CONF_FIXED_FAN_HVAC_MODES

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

# Versatile Thermostat's own built-in auto-fan. It is configured per VTherm in
# the VTherm's config entry, and any value but "auto_fan_none" makes the core
# send a fan command on every cycle (``_send_auto_fan_mode``) with no check that
# a plugin owns the fan -- the ownership check only guards the deprecated
# service. It is therefore a competing controller like the plugins above, and
# is reported under this name in ``conflicting_plugin``.
VTHERM_CONF_AUTO_FAN_MODE = "auto_fan_mode"
VTHERM_AUTO_FAN_NONE = "auto_fan_none"
NATIVE_AUTO_FAN_CONFLICT = "versatile_thermostat/auto_fan_mode"

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
# Global readiness threshold. Sized against what the sliding window can actually
# hold: with the duplicate filter below, a 0.2 degC room sensor yields roughly
# 20-30 accepted samples per day, so a 7-day window tops out near 180 samples.
# The previous 240 was unreachable on real hardware and is_ready() never flipped.
MIN_SAMPLES_LEARNING = 120  # Minimum slope samples required for initial readiness
MIN_MODE_PROFILE_SAMPLES = 10  # Minimum *measured* samples per fan mode to trust its profile
# Newest samples always kept per (fan_mode, hvac_mode) profile, however old they
# are. The reliability gate above counts measured samples, and a rarely-used
# speed collects a handful per week: expiring them by date alone (the 7-day
# window) meant a profile could never accumulate ten, and lost each week what it
# had gathered the week before. Retention by count lets it build up across weeks.
PROFILE_RETENTION_SAMPLES = 40
REFERENCE_SLOPE_ERROR = 1.0  # °C – reference comfort error at which the representative
# "working" effective slope is reported. The learned slope model is slope(error) = a + b·error;
# evaluating it at this gap yields a value reflecting real cooling/heating power rather than the
# near-equilibrium median, which is structurally diluted by samples taken close to the setpoint.
SETPOINT_DROP_LEARNING_COOLDOWN = 30.0  # Minutes to block learning after a setpoint drop
# Minimum factor x dead_time the fan mode must be active before learning. Equal to
# DEAD_TIME_SAFETY_FACTOR on purpose: the phase gate already requires ESTABLISHED
# (1.5 x dead_time), and a stricter second factor only pushed the first sample
# past the point where the controller is allowed to change speed again.
MIN_ESTABLISHED_RATIO = DEAD_TIME_SAFETY_FACTOR

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
