"""``arenaonair install-app``: a clickable ArenaOnAir with its own icon.

macOS gets ~/Applications/ArenaOnAir.app (open it, then "Keep in Dock"),
Linux an application-menu entry, Windows a Start-menu shortcut. Each starts
this Python environment with the status window. Re-run after moving the
checkout or the environment.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__

APP_NAME = "ArenaOnAir"
BUNDLE_ID = "com.arenaonair.ArenaOnAir"
ICON_PNG = Path(__file__).resolve().parent / "assets" / "icon.png"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
              "LaunchServices.framework/Support/lsregister")


def source_dir() -> Path | None:
    """Directory to put on PYTHONPATH, or None when the package is installed."""
    parent = Path(__file__).resolve().parent.parent
    return None if parent.name in ("site-packages", "dist-packages") else parent


# -- macOS ------------------------------------------------------------------

def macos_launcher(python: str, source: Path | None) -> str:
    """Bundle executable. It execs Python so the Dock tile stays this app's."""
    q = shlex.quote
    return f"""#!/bin/bash
# Written by 'arenaonair install-app'; re-run it after moving the checkout or venv.
PYTHON={q(python)}
SOURCE={q(str(source) if source else "")}
LOG_DIR="${{ARENAONAIR_DATA_DIR:-$HOME/.arenaonair}}/logs"
fail() {{
  /usr/bin/osascript -e 'on run argv' \\
    -e 'display alert "ArenaOnAir can’t start" message (item 1 of argv) as critical' \\
    -e 'end run' "$1" >/dev/null 2>&1
  exit 1
}}
[ -x "$PYTHON" ] || fail "Its Python environment is missing: $PYTHON. Run ./run.sh install-app again."
if [ -n "$SOURCE" ]; then
  [ -d "$SOURCE" ] || fail "The ArenaOnAir folder isn’t available: $SOURCE. Is that drive connected?"
  export PYTHONPATH="$SOURCE${{PYTHONPATH:+:$PYTHONPATH}}"
fi
"$PYTHON" -c 'import arenaonair, PySide6' 2>/dev/null \\
  || fail "The status window needs the ui extra. In the ArenaOnAir folder run: uv pip install --python $PYTHON -e '.[ui,tts]'"
mkdir -p "$LOG_DIR"
if [ -f "$LOG_DIR/app.log" ] && [ "$(/usr/bin/stat -f%z "$LOG_DIR/app.log")" -gt 5000000 ]; then
  mv -f "$LOG_DIR/app.log" "$LOG_DIR/app.log.1"
fi
export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"
export ARENAONAIR_APP_BUNDLE=1
cd "$HOME" || exit 1
exec "$PYTHON" -m arenaonair.app --ui >>"$LOG_DIR/app.log" 2>&1
"""


def macos_info_plist() -> dict:
    return {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": APP_NAME,
        "CFBundleIconFile": "AppIcon",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSApplicationCategoryType": "public.app-category.entertainment",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
    }


def build_icns(png: Path, out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale, suffix in ((1, ""), (2, "@2x")):
                px = str(size * scale)
                subprocess.run(["sips", "-z", px, px, str(png), "--out",
                                str(iconset / f"icon_{size}x{size}{suffix}.png")], check=True, capture_output=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True, capture_output=True)


def install_macos(dest: Path, python: str, source: Path | None) -> Path:
    app = dest / f"{APP_NAME}.app"
    if app.exists():
        try:
            ours = plistlib.loads((app / "Contents" / "Info.plist").read_bytes()).get("CFBundleIdentifier") == BUNDLE_ID
        except (OSError, plistlib.InvalidFileException):
            ours = False
        if not ours:
            raise FileExistsError(f"{app} exists and wasn't made by install-app; move it away first")
        shutil.rmtree(app)
    macos_dir = app / "Contents" / "MacOS"
    resources = app / "Contents" / "Resources"
    macos_dir.mkdir(parents=True)
    resources.mkdir()
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(macos_info_plist()))
    launcher = macos_dir / APP_NAME
    launcher.write_text(macos_launcher(python, source), encoding="utf-8")
    launcher.chmod(0o755)
    build_icns(ICON_PNG, resources / "AppIcon.icns")
    # Refresh Launch Services so Finder and the Dock pick up the new icon now.
    subprocess.run([LSREGISTER, "-f", str(app)], capture_output=True)
    return app


# -- Linux ------------------------------------------------------------------

def _desktop_arg(arg: str) -> str:
    """Quote one Exec= argument per the Desktop Entry spec."""
    arg = arg.replace("%", "%%")
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    return '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'


def linux_desktop_entry(python: str, source: Path | None, icon: Path) -> str:
    argv = (["env", f"PYTHONPATH={source}"] if source else []) + [python, "-m", "arenaonair.app", "--ui"]
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={APP_NAME}",
        "Comment=Radio-style play-by-play for MTG Arena",
        "Exec=" + " ".join(_desktop_arg(a) for a in argv),
        f"Icon={icon}",
        "Terminal=false",
        "Categories=Game;AudioVideo;",
    ]) + "\n"


def install_linux(python: str, source: Path | None) -> Path:
    apps = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    entry = apps / "arenaonair.desktop"
    entry.write_text(linux_desktop_entry(python, source, ICON_PNG), encoding="utf-8")
    entry.chmod(0o755)
    return entry


# -- Windows ----------------------------------------------------------------

def ico_bytes(png: Path, sizes=(16, 24, 32, 48, 64, 128, 256)) -> bytes:
    """A multi-size .ico whose entries are PNGs (supported since Windows Vista)."""
    from PySide6.QtCore import QBuffer, QIODevice, Qt
    from PySide6.QtGui import QImage

    source = QImage(str(png))
    if source.isNull():
        raise ValueError(f"could not read {png}")
    images = []
    for size in sizes:
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        source.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation).save(buf, "PNG")
        images.append((size, bytes(buf.data())))
    offset = 6 + 16 * len(images)
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = data = b""
    for size, blob in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(blob), offset + len(data))
        data += blob
    return header + entries + data


def install_windows(python: str, source: Path | None) -> Path:
    pythonw = Path(python).with_name("pythonw.exe")
    target = str(pythonw if pythonw.is_file() else python)
    # A checkout's root has an import shim, so -m works from there without PYTHONPATH.
    workdir = source.parent if source and (source.parent / "arenaonair" / "__init__.py").is_file() else Path.home()
    from .config import data_dir
    icon = data_dir() / "ArenaOnAir.ico"
    icon.parent.mkdir(parents=True, exist_ok=True)
    icon.write_bytes(ico_bytes(ICON_PNG))
    programs = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    link = programs / f"{APP_NAME}.lnk"
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:AOA_LINK); "
              "$s.TargetPath = $env:AOA_TARGET; $s.Arguments = '-m arenaonair.app --ui'; "
              "$s.WorkingDirectory = $env:AOA_WORKDIR; $s.IconLocation = $env:AOA_ICON; "
              "$s.Description = 'Radio-style play-by-play for MTG Arena'; $s.Save()")
    env = {**os.environ, "AOA_LINK": str(link), "AOA_TARGET": target,
           "AOA_WORKDIR": str(workdir), "AOA_ICON": str(icon)}
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   env=env, check=True, capture_output=True)
    return link


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="arenaonair install-app", description=__doc__.splitlines()[0])
    parser.add_argument("--dest", type=Path, default=Path.home() / "Applications",
                        help="macOS: folder for ArenaOnAir.app (default ~/Applications)")
    args = parser.parse_args(argv)
    python, source = sys.executable, source_dir()
    try:
        if sys.platform == "darwin":
            path = install_macos(args.dest.expanduser(), python, source)
            print(f"Installed {path}\nOpen it (or: open {shlex.quote(str(path))}), then right-click its "
                  "Dock icon > Options > Keep in Dock.")
        elif sys.platform == "win32":
            path = install_windows(python, source)
            print(f"Installed {path}\nFind ArenaOnAir in the Start menu; right-click it to pin it to the taskbar.")
        else:
            path = install_linux(python, source)
            print(f"Installed {path}\nArenaOnAir is now in your application menu.")
    except (OSError, ValueError, subprocess.CalledProcessError, ImportError) as exc:
        print(f"Could not install the app: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
