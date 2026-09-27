# macOS setup

Follow the [README installation steps](../README.md#install) for a fresh Mac.
They install uv, Python 3.12, the status window, and neural voices, with no access
to a developer's machine or shared credentials required.

After extracting or cloning the source and installing uv, run from the source folder:

```sh
bash ./run.sh setup
bash ./run.sh build-carddb
bash ./run.sh doctor
bash ./run.sh install-app
bash ./run.sh --ui
```

Enable Arena's **Options → Account → Detailed Logs (Plugin Support)** and restart
Arena before setup. Choose five free hosted matches, your Patreon key, or your own provider. Choose
**later** to connect through the desktop dialog. Setup may report missing card
data until `build-carddb` finishes.

Open `~/Applications/ArenaOnAir.app` for subsequent launches and choose **Options →
Keep in Dock** from its Dock icon. Click **Hear the booth** before starting a match
to test the selected voices; the first initialization downloads model files.

The Python environment is on local disk at `~/.venvs/arenaonair`, even if the
checkout is on a network share. Keep both available; re-run `install-app` after
moving either. Manual installation requires Python **3.11+**, with **3.12** recommended.

See [troubleshooting and updates](install.md), [connection setup](llm-booth.md#connect-your-model),
and the [usage guide](usage.md). Developers can find the test and replay commands
in [Development](../README.md#development).
