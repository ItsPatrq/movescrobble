"""macOS launchd agent: run `movescrobble on-connect` whenever /Volumes changes."""
from __future__ import annotations

import plistlib
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

LABEL = "com.movescrobble.onconnect"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(python: str = sys.executable, volumes: str = "/Volumes", log_dir: Path | None = None) -> bytes:
    log_dir = log_dir or Path.home() / "Library" / "Logs"
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [python, "-m", "movescrobble", "on-connect"],
        "WatchPaths": [volumes],
        "ThrottleInterval": 30,
        "RunAtLoad": False,
        "StandardOutPath": str(log_dir / "movescrobble.log"),
        "StandardErrorPath": str(log_dir / "movescrobble.log"),
    })


def install(uid: int, path: Path = PLIST, run: Callable = subprocess.run, **kw) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_plist(**kw))
    run(["launchctl", "bootout", f"gui/{uid}", str(path)], capture_output=True)  # idempotent reinstall
    run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], check=True)
    return path


def uninstall(uid: int, path: Path = PLIST, run: Callable = subprocess.run) -> None:
    run(["launchctl", "bootout", f"gui/{uid}", str(path)], capture_output=True)
    path.unlink(missing_ok=True)
