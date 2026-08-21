#!/bin/bash
#
# Install Versatile Thermostat into the dev Home Assistant config.
#
# This plugin is not a standalone integration: it registers a Feature Manager
# with VTherm's API and is handed a live thermostat runtime. There is nothing to
# run without VTherm actually present, so the dev container installs it rather
# than faking it.
#
# VTherm is cloned (not vendored) so the container tracks upstream, and pinned to
# a tag known to expose the external Feature Manager API. Override with:
#   VTHERM_REF=main ./scripts/install_vtherm.sh

set -e

# First release exposing InterfaceFeatureManagerFactory / vtherm_api >= 0.4.0.
VTHERM_REF="${VTHERM_REF:-main}"
VTHERM_REPO="${VTHERM_REPO:-https://github.com/jmcollin78/versatile_thermostat.git}"

cd "$(dirname "$0")/.."
ROOT="${PWD}"
CACHE="${ROOT}/.vtherm_src"
TARGET="${ROOT}/config/custom_components/versatile_thermostat"

mkdir -p "${ROOT}/config/custom_components"

if [ ! -d "${CACHE}/.git" ]; then
    echo "Cloning Versatile Thermostat (${VTHERM_REF})..."
    git clone --depth 1 --branch "${VTHERM_REF}" "${VTHERM_REPO}" "${CACHE}"
else
    echo "Updating Versatile Thermostat checkout..."
    git -C "${CACHE}" fetch --depth 1 origin "${VTHERM_REF}"
    git -C "${CACHE}" checkout -q FETCH_HEAD
fi

# Symlink rather than copy so `git -C .vtherm_src log` still explains what is
# installed when a VTherm-side behaviour needs checking.
rm -rf "${TARGET}"
ln -s "${CACHE}/custom_components/versatile_thermostat" "${TARGET}"

# VTherm declares these itself, but HA only auto-installs requirements for
# integrations it discovers through HACS, not for one dropped in by hand.
echo "Installing VTherm runtime requirements..."
python3 - "${CACHE}/custom_components/versatile_thermostat/manifest.json" <<'PY'
import json
import subprocess
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
requirements = manifest.get("requirements", [])
if requirements:
    print("  ->", ", ".join(requirements))
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", *requirements])
PY

VERSION=$(python3 -c "import json,sys;print(json.load(open('${CACHE}/custom_components/versatile_thermostat/manifest.json'))['version'])")
echo "Versatile Thermostat ${VERSION} installed at config/custom_components/versatile_thermostat"
