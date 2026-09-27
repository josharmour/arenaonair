"""Exercise an installed package from outside its checkout, with disposable data.

Run with the installed environment's ``python -I tools/install_smoke.py``.
Add ``--voices`` to download the Kokoro model and synthesize both caster voices
without playing audio. Does not use real logs, model credentials, or user config.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voices", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="arenaonair-smoke-") as tmp:
        root = Path(tmp)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ARENAONAIR_") and k != "PYTHONPATH"}
        env.update(ARENAONAIR_DATA_DIR=str(root / "data"), QT_QPA_PLATFORM="offscreen")
        env["HF_HOME"] = str(root / "models")
        for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
            env.pop(name, None)

        def cli(*arguments, input=None, expected=0):
            result = subprocess.run([sys.executable, "-I", "-m", "arenaonair.app", *arguments],
                                    cwd=root, env=env, input=input, text=True,
                                    capture_output=True, timeout=90)
            if result.returncode != expected:
                raise RuntimeError(f"Command {arguments} exited {result.returncode}\n"
                                   f"{result.stdout}\n{result.stderr}")
            return result.stdout

        for command in ((), ("setup",), ("doctor",), ("build-carddb",), ("install-app",), ("recap",)):
            assert "usage:" in cli(*command, "--help")
        log = root / "Player.log"
        log.write_text("DETAILED LOGS: ENABLED\n", encoding="utf-8")
        config = root / "config.toml"
        config.write_text(f"log_path = {json.dumps(str(log))}\n", encoding="utf-8")
        # Config is preseeded only with a synthetic log path, preserved by setup.
        # Card-data diagnostics can fail until the database is built below.
        setup = subprocess.run([sys.executable, "-I", "-m", "arenaonair.app", "setup", "--config", str(config)],
                               cwd=root, env=env, input="classic\ny\nlater\nn\n", text=True,
                               capture_output=True, timeout=90)
        assert setup.returncode in (0, 1), setup.stderr
        assert "Wrote " in setup.stdout, setup.stdout + setup.stderr
        import tomllib
        saved = tomllib.loads(config.read_text())
        assert saved["log_path"] == str(log)
        assert saved["broadcast"]["mode"] == "dual"
        assert "llm" not in saved

        cards = root / "cards.json"
        cards.write_text(json.dumps([{
            "arena_id": 1, "name": "Plains", "type_line": "Basic Land — Plains",
            "mana_cost": "", "oracle_text": "{T}: Add {W}.",
        }]), encoding="utf-8")
        db = root / "cards.sqlite"
        cli("build-carddb", "--from-file", str(cards), "--out", str(db))
        config.write_text(f"carddb_path = {json.dumps(str(db))}\n" + config.read_text(), encoding="utf-8")
        assert "Connection required" in cli("doctor", "--config", str(config), expected=1)
        cli("--config", str(config), "--dry-run", "--once", "--no-ui", "--narration-mode", "legacy")

        # Run the window from installed resources, with no display or user writes.
        code = '''
from pathlib import Path
from PySide6.QtWidgets import QApplication
from arenaonair.app import ArenaOnAirApp
from arenaonair.config import Config
from arenaonair.desktop import ICON_PNG
from arenaonair.diagnostics import BugReportLogs
from arenaonair.ui import BroadcastWindow
import sys
assert ICON_PNG.is_file(), ICON_PNG
qt = QApplication([])
app = ArenaOnAirApp(Config(log_path=sys.argv[1], carddb_path=sys.argv[2], history_enabled=False), dry_run=True)
app.config_path = Path(sys.argv[3])
window = BroadcastWindow(app, BugReportLogs())
window.show()
qt.processEvents()
assert window.isVisible()
window.timer.stop()
window.close()
app.stop()
'''
        subprocess.run([sys.executable, "-I", "-c", code, str(log), str(db), str(config)],
                       cwd=root, env=env, check=True, timeout=90)
        print("Installed package: help, setup, card builder, doctor, launch, window, and icon passed.", flush=True)
        if args.voices:
            code = '''
from arenaonair.platform.tts import KokoroEngine
engine = KokoroEngine()
for voice in ("am_adam", "am_onyx"):
    chunks = list(engine._synthesize_pcm("Welcome to Arena on Air.", voice=voice))
    assert chunks and sum(len(pcm) for pcm, sr in chunks) > 2400, voice
    assert all(sr == 24000 for pcm, sr in chunks)
    print(f"Synthesized {voice}: {sum(len(pcm) for pcm, sr in chunks)} samples", flush=True)
engine.shutdown()
'''
            subprocess.run([sys.executable, "-I", "-c", code], cwd=root, env=env, check=True, timeout=600)
        print("Installation smoke check passed.")


if __name__ == "__main__":
    main()
