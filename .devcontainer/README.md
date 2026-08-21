## Developing with Visual Studio Code + devcontainer

The easiest way to get started with custom integration development is to use Visual Studio Code with devcontainers. This approach will create a preconfigured development environment with all the tools you need.

In the container you will have a dedicated Home Assistant core instance running with your custom component code. You can configure this instance by updating the `./devcontainer/configuration.yaml` file.

---

## The test bench

This plugin is not a standalone integration: it registers a Feature Manager with
Versatile Thermostat's API and is handed a live thermostat runtime. There is
nothing to exercise without VTherm actually present, so the container installs
it rather than faking it.

Starting Home Assistant (`./container start`) builds this automatically:

| Piece | What it is |
| --- | --- |
| `versatile_thermostat` | Cloned from upstream into `config/custom_components` by `scripts/install_vtherm.sh`, with its own requirements installed. Override the ref with `VTHERM_REF=... ./scripts/install_vtherm.sh`. |
| `climate.mock_ac` | A fake air conditioner exposing five manual fan speeds (`silent`, `low`, `medium`, `high`, `turbo`) plus `auto`. |
| `sensor.room_temp` | The simulated room, advanced every 30 s by the thermal model in `configuration.yaml`. |
| `sensor.outdoor_temp` | What the room leaks towards. Default 32 °C. |
| `sensor.sim_cooling_power` | The cooling the mock AC is delivering right now, so the ladder being simulated can be compared against the one the plugin learns. |

### Nothing left to configure

Neither the VTherm nor this plugin can be declared in YAML — both are
config-entry integrations, and VTherm's config flow is an interactive menu with
no import step. So the `dev_bootstrap` dev-only component creates both entries
through Home Assistant's config-entry API once the container has started:

- `climate.mock_room`, an `over_climate` VTherm on `climate.mock_ac`, in AC mode,
  with VTherm's own auto-fan **off** (this plugin owns the fan; VTherm's auto-fan
  would fight it for the actuator);
- this plugin, attached to that VTherm, with the ladder set explicitly to
  `silent, low, medium, high, turbo`.

Set `climate.mock_room` to `cool` at 22 °C and it starts controlling.

The bootstrap is idempotent and only fills in what is missing: edits you make in
the UI survive a restart, and deleting an entry recreates it on the next start.
To drive the setup by hand instead, drop `dev_bootstrap:` from
`configuration.yaml` and add both integrations through the UI.

Entries added this way skip the config flow, so nothing supplies the defaults
the UI would have. The VTherm data is therefore built from VTherm's own
`const` module — an upstream rename fails loudly at import rather than producing
a subtly broken entry — and its key set is kept aligned with the full
`over_climate` fixture in VTherm's test suite.

### What the bench is calibrated to reproduce

The fan ladder is deliberately spaced so the weak speeds *cannot* hold the
setpoint against the envelope, which is the situation the controller's guards
exist for. At the defaults (`k_env = 0.08 /h`, outdoor 32 °C, setpoint 22 °C):

| Fan | Cooling (°C/h) | Settles at | Holds 22 °C? |
| --- | --- | --- | --- |
| `silent` | -0.15 | 30.1 °C | no |
| `low` | -0.35 | 27.6 °C | no |
| `medium` | -0.60 | 24.5 °C | no |
| `high` | -0.90 | 20.8 °C | yes |
| `turbo` | -1.30 | 15.8 °C | yes |

Three speeds that lose ground, mirroring the production deployment this was
built from. The effective slopes the plugin should converge on, measured at its
1 °C reference gap, are roughly `-0.57 / -0.37 / -0.12 / +0.18 / +0.58`.

Turn `input_number.sim_k_env` down to make every speed adequate, or `sim_outdoor_temp`
up to starve even `turbo`.

### Making it move faster

Learning is gated on elapsed time (dead time, minimum interval, the
`ESTABLISHED` phase), so a bench left running at real speed takes hours to teach
the model anything. To exercise a specific behaviour instead, drag
`input_number.sim_room_temp` to force a large comfort error, or call
`vtherm_mpc_fan.set_effective_slope` to seed a profile directly.

**Prerequisites**

- [git](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git)
- Docker
  -  For Linux, macOS, or Windows 10 Pro/Enterprise/Education use the [current release version of Docker](https://docs.docker.com/install/)
  -   Windows 10 Home requires [WSL 2](https://docs.microsoft.com/windows/wsl/wsl2-install) and the current Edge version of Docker Desktop (see instructions [here](https://docs.docker.com/docker-for-windows/wsl-tech-preview/)). This can also be used for Windows Pro/Enterprise/Education.
- [Visual Studio code](https://code.visualstudio.com/)
- [Remote - Containers (VSC Extension)][extension-link]

[More info about requirements and devcontainer in general](https://code.visualstudio.com/docs/remote/containers#_getting-started)

[extension-link]: https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers

**Getting started:**

1. Fork the repository.
2. Clone the repository to your computer.
3. Open the repository using Visual Studio code.

When you open this repository with Visual Studio code you are asked to "Reopen in Container", this will start the build of the container.

_If you don't see this notification, open the command palette and select `Remote-Containers: Reopen Folder in Container`._

### Tasks

The devcontainer comes with some useful tasks to help you with development, you can start these tasks by opening the command palette and select `Tasks: Run Task` then select the task you want to run.

When a task is currently running (like `Run Home Assistant on port 9123` for the docs), it can be restarted by opening the command palette and selecting `Tasks: Restart Running Task`, then select the task you want to restart.

The available tasks are:

Task | Description
-- | --
Run Home Assistant on port 9123 | Launch Home Assistant with your custom component code and the configuration defined in `.devcontainer/configuration.yaml`.
Run Home Assistant configuration against /config | Check the configuration.
Upgrade Home Assistant to latest dev | Upgrade the Home Assistant core version in the container to the latest version of the `dev` branch.
Install a specific version of Home Assistant | Install a specific version of Home Assistant core in the container.

### Step by Step debugging

With the development container,
you can test your custom component in Home Assistant with step by step debugging.

You need to modify the `configuration.yaml` file in `.devcontainer` folder
by uncommenting the line:

```yaml
# debugpy:
```

Then launch the task `Run Home Assistant on port 9123`, and launch the debbuger
with the existing debugging configuration `Python: Attach Local`.

For more information, look at [the Remote Python Debugger integration documentation](https://www.home-assistant.io/integrations/debugpy/).
