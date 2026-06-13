"""Locate and run kicad-cli."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys


class KicadCliError(RuntimeError):
    pass


_MAC_CANDIDATES = [
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    os.path.expanduser("~/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
    "/Applications/KiCad.app/Contents/MacOS/kicad-cli",
]


def find_kicad_cli() -> str:
    env = os.environ.get("VIADUCT_KICAD_CLI")
    if env:
        if os.path.isfile(env):
            return env
        raise KicadCliError(f"VIADUCT_KICAD_CLI is set to {env!r} but that file does not exist")
    found = shutil.which("kicad-cli")
    if found:
        return found
    if sys.platform == "darwin":
        for c in _MAC_CANDIDATES:
            if os.path.isfile(c):
                return c
    elif sys.platform.startswith("win"):
        for c in sorted(glob.glob(r"C:\Program Files\KiCad\*\bin\kicad-cli.exe"), reverse=True):
            return c
    else:
        for c in ("/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli", "/snap/bin/kicad.kicad-cli"):
            if os.path.isfile(c):
                return c
    raise KicadCliError(
        "kicad-cli not found. Install KiCad (https://kicad.org), or set the "
        "VIADUCT_KICAD_CLI environment variable to the full path of kicad-cli "
        "(macOS: /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli)."
    )


def run(args: list[str], timeout: float = 300) -> str:
    """Run kicad-cli with *args*; returns stdout+stderr text, raises on failure."""
    cli = find_kicad_cli()
    proc = subprocess.run(
        [cli, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise KicadCliError(
            f"kicad-cli {' '.join(args)} failed (exit {proc.returncode}):\n{output.strip()}"
        )
    return output


def version() -> str:
    return run(["version"]).strip()
