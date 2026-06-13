"""Edit safety: KiCad lockfile detection and .bak backups."""

from __future__ import annotations

import os
import shutil


class BoardLockedError(RuntimeError):
    pass


class BackupError(RuntimeError):
    pass


def lockfile_path(file_path: str) -> str:
    """KiCad's lockfile for /dir/foo.kicad_pcb is /dir/~foo.kicad_pcb.lck."""
    d, name = os.path.split(os.path.abspath(file_path))
    return os.path.join(d, f"~{name}.lck")


def check_not_locked(file_path: str) -> None:
    lck = lockfile_path(file_path)
    if os.path.exists(lck):
        raise BoardLockedError(
            f"Refusing to edit: {os.path.basename(file_path)} appears to be open in "
            f"KiCad (lockfile {lck} exists). Close the file in KiCad first — editing "
            "it now would be overwritten when KiCad saves. If KiCad crashed and the "
            "lockfile is stale, delete it manually and retry."
        )


def backup_path(file_path: str) -> str:
    return file_path + ".bak"


def make_backup(file_path: str) -> str:
    """Copy *file_path* to a sibling .bak before editing. Returns backup path."""
    if not os.path.isfile(file_path):
        raise BackupError(f"cannot back up {file_path}: file does not exist")
    bak = backup_path(file_path)
    shutil.copy2(file_path, bak)
    return bak


def restore_backup(file_path: str) -> str:
    """Restore *file_path* from its sibling .bak. Returns the restored path."""
    bak = backup_path(file_path)
    if not os.path.isfile(bak):
        raise BackupError(f"no backup found at {bak}")
    check_not_locked(file_path)
    shutil.copy2(bak, file_path)
    return file_path


def guarded_write(file_path: str, text: str) -> str:
    """Lockfile check + backup + atomic-ish write. Returns the backup path."""
    check_not_locked(file_path)
    bak = make_backup(file_path)
    tmp = file_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, file_path)
    return bak
