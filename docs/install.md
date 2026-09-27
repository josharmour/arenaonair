# Installation and troubleshooting

Start with the [README installation steps](../README.md#install). They install uv,
Python 3.12, ArenaOnAir, the PySide6 window, and Kokoro neural voices. A source ZIP
works; Git is only needed if you choose to clone and pull updates.

The app supports Windows, macOS, and Linux. Linux requires Arena running through
Wine/Proton and a graphical desktop for the status window. A GPU is not required:
neural speech uses the CPU by default. Dependency and model downloads need several
GB of disk space and an internet connection on first use. Hosted generative
commentary needs internet access during play. Fully local use requires configuring
your own local language model in addition to the local voices.

## Launcher commands

Run these from the source folder, replacing `COMMAND` with `setup`, `doctor`,
`build-carddb`, `install-app`, or app flags. Omit `COMMAND` to start the app.

| System | Command |
| --- | --- |
| macOS / Linux | `bash ./run.sh COMMAND` |
| Windows PowerShell | `powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 COMMAND` |

The launchers put Python outside the checkout, in `~/.venvs/arenaonair`. They also
repair an environment missing the window or voice packages. Set `ARENAONAIR_VENV`
to an absolute local directory before running a launcher to use a different
environment. Use the same value for subsequent launches and updates.

`setup` writes `~/.arenaonair/config.toml`. Re-running it backs up the previous
file to `config.toml.bak` and writes new wizard answers. Use the window's Settings
tab for individual changes to an existing configuration.

## Manual Python installation

For users who already manage Python environments: Python **3.11+** is required;
use **3.12** for the best-tested voice installation. Run from the source folder.
Keep the environment on local disk, even if the checkout is on a network share.

macOS / Linux:

```sh
python3 -m venv "$HOME/.venvs/arenaonair"
source "$HOME/.venvs/arenaonair/bin/activate"
python -m pip install --upgrade pip
python -m pip install -e '.[tts,ui]'
arenaonair setup
arenaonair build-carddb
arenaonair doctor
arenaonair install-app
arenaonair --ui
```

Windows PowerShell (using a separately installed Python 3.12):

```powershell
py -3.12 -m venv "$HOME\.venvs\arenaonair"
$ArenaPython = "$HOME\.venvs\arenaonair\Scripts\python.exe"
& $ArenaPython -m pip install --upgrade pip
& $ArenaPython -m pip install -e '.[tts,ui]'
& $ArenaPython -m arenaonair.app setup
& $ArenaPython -m arenaonair.app build-carddb
& $ArenaPython -m arenaonair.app doctor
& $ArenaPython -m arenaonair.app install-app
& $ArenaPython -m arenaonair.app --ui
```

For a minimal terminal install, use `-e .` instead of `-e '.[tts,ui]'` and launch
with `--no-ui`. This uses available system speech: macOS `say`, Windows SAPI, or
Linux `espeak-ng`; two roles may share one physical voice. `--dry-run --no-ui`
prints commentary without audio. The convenience launchers install the full
extras, so use your environment's Python directly for a deliberately minimal install.

## Arena log discovery

Launch Arena at least once, enable **Detailed Logs (Plugin Support)** under
**Options → Account**, and restart Arena. The app discovers standard locations.
If discovery fails, choose **Player.log** in Settings or launch with
`--log-path "/full/path/to/Player.log"`.

| System | Typical location |
| --- | --- |
| Windows | `%USERPROFILE%\AppData\LocalLow\Wizards Of The Coast\MTGA\Player.log` |
| macOS | `~/Library/Logs/Wizards Of The Coast/MTGA/Player.log` |
| Linux | Inside your Wine/Proton prefix: `drive_c/users/<user>/AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log` |

For a nonstandard installation, the correct file is the one Arena updates while
you play. A single log is enough for both casters. Shared logs and relay routes
are described separately in [sharing logs](shared-logs.md).

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| `uv` is not found | Reopen the terminal after installing it. Confirm `uv --version` works; see [uv installation](https://docs.astral.sh/uv/getting-started/installation/). |
| Python/package installation fails | Use the launcher with Python 3.12, check free disk space and internet access, and read the first error above the failure. On an unsupported CPU/OS, try the minimal system-voice install above. |
| `arenaonair` is not found | Use `run.sh` / `run.ps1`, or the environment's Python with `-m arenaonair.app`. The launchers do not add global commands to your PATH. |
| `doctor` cannot find Arena's log | Start Arena once; select the log in Settings if needed. |
| Detailed logs are disabled | Enable the setting in Arena and restart Arena, then run `doctor` again. |
| Card names are missing or rules are unavailable | Run `build-carddb`. The command downloads public Scryfall data; failed builds preserve the old database. |
| Voices are slow on the first launch | Allow the Kokoro and language-model downloads to finish. Try **Hear the booth** before entering a match. |
| Kokoro/phonemizer reports missing eSpeak | Install [eSpeak NG](https://github.com/espeak-ng/espeak-ng/releases). On macOS with Homebrew use `brew install espeak-ng`; on Debian/Ubuntu use `sudo apt-get install espeak-ng`. Restart the app afterward. |
| No sound | Check the OS output device and volume; use **Hear the booth** outside a match. On Linux install `alsa-utils` and confirm `aplay` can use your output device. |
| Linux window fails to load Qt's platform plugin | Install the desktop libraries listed in the README, including `libxcb-cursor0` and `libxkbcommon-x11-0`; run in a graphical desktop session. |
| AI status shows an error or the booth stays silent | Run `doctor --online` to test the configured model (may incur an API charge), check endpoint/model/key, or open **Connect / subscription…** to change providers or subscribe. See [AI setup](llm-booth.md#connect-your-model). |
| Desktop launcher fails after moving files | Run `install-app` again from the new checkout/environment. The shortcut does not bundle Python or copy the source. |

`doctor` performs local checks by default. Its voice check confirms installation,
not audible playback; use **Hear the booth** to test sound. A missing connection must be resolved before generative commentary works. For help, use **Copy bug report** in the
window, review what it copied, and include it in a [GitHub issue](https://github.com/josharmour/arenaonair/issues).
On macOS, desktop-launch output is also saved in `~/.arenaonair/logs/app.log`.

## Update

Close ArenaOnAir. For a Git checkout, run `git pull --ff-only` in the source
folder. If you downloaded a ZIP, extract the replacement to a permanent folder
and run the commands there. Reinstall dependencies and refresh the launcher:

macOS / Linux:

```sh
uv pip install --python "$HOME/.venvs/arenaonair/bin/python" -e '.[tts,ui]'
bash ./run.sh install-app
bash ./run.sh doctor
```

Windows PowerShell:

```powershell
uv pip install --python "$HOME\.venvs\arenaonair\Scripts\python.exe" -e '.[tts,ui]'
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 install-app
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 doctor
```

Substitute your custom environment path if you set `ARENAONAIR_VENV`. Updates
keep your configuration and match history. Re-run `build-carddb` periodically for
new cards. The optional [knowledge index](llm-booth.md#local-card-knowledge) has its
own refresh command and is not required for first use.

## Files and uninstalling

`~` means your user home folder; on Windows this is usually `%USERPROFILE%`.

| Location | Contents |
| --- | --- |
| Source folder | Application code; keep it while using an editable install |
| `~/.venvs/arenaonair` | Python and installed dependencies |
| `~/.arenaonair/config.toml` | Settings |
| `~/.arenaonair/` | Match history, recaps, and app logs |
| `~/.arenaonair/trial.key`, `provider.key` | Private connection credentials written by the dialog; preserve the trial key on updates |
| `~/.config/arenaonair/llm.key` | Provider credential written by CLI setup |
| `~/.cache/arenaonair/` | Card and optional knowledge databases |
| `~/.cache/huggingface/` | Downloaded neural models; may be shared with other apps |

To uninstall, quit the app, remove its desktop launcher, then remove the source
folder and dedicated environment. The launcher is `~/Applications/ArenaOnAir.app`
on macOS, `%APPDATA%\Microsoft\Windows\Start Menu\Programs\ArenaOnAir.lnk` on Windows,
or `~/.local/share/applications/arenaonair.desktop` on Linux (under `XDG_DATA_HOME`
if customized). Keep your settings/history if you may reinstall; delete the
ArenaOnAir data, credential, and cache folders above if you want them removed.
