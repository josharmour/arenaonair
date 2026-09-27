# ArenaOnAir

A generative commentary booth for MTG Arena. Two AI casters follow your match,
call plays, and discuss the game using GLM 5.3 on our server. Their voices are
synthesized on your computer. Coaching is off by default.

**Try five matches free**, then subscribe through
[Patreon via MTGA Coach](https://mtgacoach.com/subscribe) using the same customer key.
You can also connect your own local or remote provider in the app.
Hosted commentary requires internet access and sends selected game facts to our
server. This is **not a 100% local app** unless you configure a local language model.

**Early release:** installation currently uses a source checkout and a terminal.
The steps below install the desktop window and voices, then create a clickable
launcher. Keep the checkout and its Python environment in place afterward.

## Install

You need MTG Arena installed and launched at least once, an internet connection for
installation, voice/card downloads, and hosted commentary, plus several GB of free disk space.
The launchers use [uv](https://docs.astral.sh/uv/getting-started/installation/) to
install Python 3.12 and dependencies automatically. Manual installs require Python
3.11 or newer; Python 3.12 is recommended for the voice dependencies.

### 1. Get the source and uv

Download and extract the [source ZIP](https://github.com/josharmour/arenaonair/archive/refs/heads/main.zip)
to a permanent folder, then open a terminal in the extracted `arenaonair-main`
folder. If you already use Git:

```sh
git clone https://github.com/josharmour/arenaonair.git
cd arenaonair
```

Install uv using the command for your system below. These are uv's official
installer commands; see its [installation guide](https://docs.astral.sh/uv/getting-started/installation/)
for package-manager alternatives.

**macOS — Terminal:**

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows — PowerShell:**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**Linux — terminal:**

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

On Debian/Ubuntu, also install the audio and desktop libraries:

```sh
sudo apt-get update
sudo apt-get install -y espeak-ng alsa-utils libportaudio2 libegl1 libxcb-cursor0 libxkbcommon-x11-0
```

Other Linux distributions need the equivalent packages. Arena itself needs a
working Wine/Proton installation on Linux; ArenaOnAir reads that installation's log.

**Close and reopen your terminal after installing uv**, then return to the source
folder. You do not need to install Python separately or activate a virtual environment.

### 2. Enable Arena's logs

In MTG Arena, open **Options → Account → Detailed Logs (Plugin Support)**, enable
it, and restart Arena. Leave Arena running while you complete setup.

### 3. Set up ArenaOnAir

**macOS or Linux:**

```sh
bash ./run.sh setup
bash ./run.sh build-carddb
bash ./run.sh doctor
bash ./run.sh install-app
bash ./run.sh --ui
```

**Windows — PowerShell:**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 setup
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 build-carddb
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 doctor
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 install-app
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 --ui
```

The Windows command permits this script for that process only; it does not change
your saved execution policy. Run each command in order. The first installs the app,
window, and neural voices under `~/.venvs/arenaonair` (`%USERPROFILE%\.venvs\arenaonair`
on Windows). Initial installation can take several minutes.

During setup, choose a persona and one or two commentators. Select **trial** for
five hosted matches, **premium** to enter your Patreon key, **custom** for your own
provider, or **later** to use the desktop connection dialog. OBS setup is optional.
Setup may flag missing card data; the next command downloads it from Scryfall
(several hundred MB). Re-run `doctor` and fix any `[x ]` problems before playing.
If you chose **later**, connect in the app before expecting commentary.

### 4. Hear your first match

In the window, click **Hear the booth** while outside a match. The first voice
initialization downloads model files and can take a few minutes. You should hear
the selected casters introduce themselves. Start a match in Arena; the app should
move from **watching** to **in_match** and speak the plays. Silence between events
is normal. ArenaOnAir does not launch Arena for you.

Next time, use the launcher created by `install-app`:

| System | Where to open ArenaOnAir |
| --- | --- |
| macOS | `~/Applications/ArenaOnAir.app`; keep its icon in the Dock if desired |
| Windows | **Start → ArenaOnAir** |
| Linux | **ArenaOnAir** in your applications menu |

If you move the source folder or environment, run `install-app` again from the new
location. See [installation, updates, and troubleshooting](docs/install.md) for
manual Python setup, log locations, audio problems, and uninstalling.

## Connect the generative booth

Click **Connect / subscription…** in the app:

| Connection | What you need |
| --- | --- |
| **Five free matches** | Internet access; no payment details. Hosted GLM 5.3 is preconfigured. |
| **Premium** | Subscribe at [mtgacoach.com/subscribe](https://mtgacoach.com/subscribe), link Patreon, and paste the issued key. Existing patrons can use their current key. |
| **My own provider** | Enter an endpoint and optional port, load/select a model, and enter a key only if required. **Find local providers** checks common ports on your computer. |

Click **Connect and restart** to apply the connection. The trial counter lives on
the server: a match counts on its first successful model response. App restarts,
voice previews, and reconnects to the same match do not count again; best-of-three
games share one match. Each trial match session allows up to four hours and 1,200
model requests. Five matches are offered per device trial; reinstalling does not
reset the server counter. Keep `~/.arenaonair/trial.key` to reconnect to your trial.

Custom providers must support chat completions and structured JSON-schema output;
quality and latency depend on the model. Local-only generation requires a running
local model and the hardware to serve it. Remote providers may charge for usage.
If the connection fails, the booth shows an error and waits to reconnect.
See [connection setup and privacy](docs/llm-booth.md#connect-your-model).

## Using the app

- **Booth:** choose one or two casters, their voices, and the balance of calls and analysis.
- **Settings:** change personas, pacing, Arena's log path, optional coaching, AI connection, and OBS settings.
- **Recaps and history:** stored locally under `~/.arenaonair/`.
- **Streaming:** optional captions/scoreboard overlay and controls for whether your hand is spoken.
- **Copy bug report:** copies diagnostics to your clipboard for review; it does not upload them.

See the [usage guide](docs/usage.md), [sharing logs for spectators](docs/shared-logs.md),
and [troubleshooting](docs/install.md#troubleshooting).

## Development

Use a separate local Python environment, especially when the checkout is on a
network drive. Install the developer dependencies, then run the tests and replays:

```sh
uv venv --python 3.12 ~/.venvs/arenaonair-dev
uv pip install --python ~/.venvs/arenaonair-dev/bin/python -e '.[dev]'
~/.venvs/arenaonair-dev/bin/python -m pytest
~/.venvs/arenaonair-dev/bin/python tools/replay_harness.py --fixture fixtures/matches/match_02.jsonl --determinism
~/.venvs/arenaonair-dev/bin/python tools/replay_harness.py --fixture fixtures/matches/match_01.jsonl --determinism
```

On Windows, use `$HOME\.venvs\arenaonair-dev\Scripts\python.exe` for that environment.
CI runs tests on Windows, macOS, and Linux, plus installation checks from a built
wheel. See [requirements](docs/PRD.md) and [design](docs/DESIGN.md) for project internals.
