# ArenaOnAir

Radio-style commentary for your MTG Arena matches. ArenaOnAir reads Arena's
`Player.log` and speaks the action with one or two commentators. A desktop window
lets you change voices, commentary style, and streaming settings.

**Works without an AI account:** built-in scripted commentary and local neural
voices are included in the recommended install. Optional AI commentary uses a
compatible model provider and your own API credentials. Coaching is off by default.

**Early release:** installation currently uses a source checkout and a terminal.
The steps below install the desktop window and voices, then create a clickable
launcher. Keep the checkout and its Python environment in place afterward.

## Install

You need MTG Arena installed and launched at least once, an internet connection for
installation and the initial voice/card downloads, and several GB of free disk space.
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

During setup, choose a persona and one or two commentators. Choose **no** when
asked about an AI model to start with built-in commentary; you can connect a model
later in Settings. OBS setup is also optional. Setup ends with diagnostics and
may report missing card data; the next command downloads it from Scryfall
(several hundred MB). Re-run `doctor` and fix any `[x ]` problems before playing.
The optional AI warning is expected when you use built-in commentary.

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

## Optional AI commentary

The built-in booth is ready without an API key. For original AI commentary, you
need your own endpoint, model name, and API key from a compatible provider. API
usage may cost money, depending on that provider. There is no bundled hosted
account or shared credential.

Follow [AI booth setup](docs/llm-booth.md#connect-your-model). The model must support
chat completions with structured JSON-schema output; compatibility and live
latency vary. If a configured model fails, its commentary stays silent and the
window shows the error. Select **Scripted booth** under Settings → Commentary → Writer to return to scripted
commentary.

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
