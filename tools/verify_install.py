"""Fresh launcher + wheel installation check, used on all three OSes in CI."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="arenaonair install ") as tmp:
        root = Path(tmp)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ARENAONAIR_") and k != "PYTHONPATH"}
        env["ARENAONAIR_VENV"] = str(root / "venv")
        launcher = (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(repo / "run.ps1")]
                    if sys.platform == "win32" else ["bash", str(repo / "run.sh")])
        # Work outside the checkout, including a space in the environment path.
        subprocess.run([*launcher, "--help"], cwd=root, env=env, check=True)
        repeat = subprocess.run([*launcher, "--help"], cwd=root, env=env,
                                capture_output=True, text=True)
        assert repeat.returncode == 0, repeat.stdout + repeat.stderr
        assert "Installing ArenaOnAir" not in repeat.stdout, repeat.stdout
        python = root / "venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        out = root / "dist"
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out), str(repo)], env=env, check=True)
        wheel, = out.glob("*.whl")
        subprocess.run(["uv", "pip", "install", "--python", str(python), "--reinstall-package", "arenaonair",
                        str(wheel) + "[tts,ui]"], env=env, check=True)
        subprocess.run([str(python), "-I", str(repo / "tools" / "install_smoke.py"), *sys.argv[1:]],
                       cwd=root, env=env, check=True)


if __name__ == "__main__":
    main()
