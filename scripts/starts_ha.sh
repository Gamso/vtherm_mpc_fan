#!/bin/bash

set -e
set -x

cd "$(dirname "$0")/.."
pwd

# `config` exists but is not a directory: a leftover file, or a symlink whose
# target is gone -- which is what a bind mount looks like after the host side was
# deleted underneath a running container. `mkdir -p` would fail with the
# unhelpful "File exists" (it only tolerates an existing *directory*), and
# `set -e` would then abort before Home Assistant ever starts, so clear it here.
if [ -e "${PWD}/config" ] || [ -L "${PWD}/config" ]; then
    if [ ! -d "${PWD}/config" ]; then
        echo "config exists but is not a directory; removing it" >&2
        rm -rf "${PWD}/config"
    fi
fi

# Create config dir if not present
if [[ ! -d "${PWD}/config" ]]; then
    mkdir -p "${PWD}/config"
    # Add defaults configuration
    hass --config "${PWD}/config" --script ensure_config
fi

# Overwrite configuration.yaml if provided
if [ -f ${PWD}/.devcontainer/configuration.yaml ]; then
    rm -f ${PWD}/config/configuration.yaml
    ln -s ${PWD}/.devcontainer/configuration.yaml ${PWD}/config/configuration.yaml
fi

# Dev-only custom_components (climate_template)
if [ ! -d ${PWD}/config/custom_components ]; then
    mkdir -p ${PWD}/config/custom_components
fi

for dev_component in climate_template dev_bootstrap; do
    if [ ! -e ${PWD}/config/custom_components/${dev_component} ]; then
        rm -f ${PWD}/config/custom_components/${dev_component}
        ln -s ${PWD}/.devcontainer/${dev_component} \
              ${PWD}/config/custom_components/${dev_component}
    fi
done

# Versatile Thermostat itself. This plugin registers a Feature Manager with
# VTherm's API and is handed a live thermostat runtime, so there is nothing to
# exercise without the real thing -- the previous approach of faking VTherm by
# injecting a `temperature_slope` attribute onto a mock climate only worked back
# when the controller scraped entity attributes.
# Invoked through bash rather than executed directly: the repo is developed on
# Windows, where the executable bit is easily lost, and `set -e` above would turn
# a "Permission denied" into Home Assistant silently never starting.
if [ ! -e ${PWD}/config/custom_components/versatile_thermostat ]; then
    bash ${PWD}/scripts/install_vtherm.sh
fi

# Set the path to custom_components
## This let's us have the structure we want <root>/custom_components/integration_blueprint
## while at the same time have Home Assistant configuration inside <root>/config
## without resulting to symlinks.
export PYTHONPATH="${PWD}:${PWD}/config:${PYTHONPATH}"

# Start Home Assistant
hass --config "${PWD}/config" --debug