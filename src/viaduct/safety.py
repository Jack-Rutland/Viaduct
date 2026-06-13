"""Edit safety: KiCad lockfile detection and .bak backups."""

from __future__ import annotations

import glob
import os
import re
import shutil

HISTORY_SUFFIX = ".vbak"  # rolling numbered backups: foo.kicad_pcb.vbak0001, ...
HISTORY_KEEP = 10


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
    """Lockfile check + backup + atomic-ish write. Returns the .bak path.

    Also appends the pre-edit state to the rolling numbered history so several
    edits can be undone (see :func:`list_backups` / :func:`restore_to`).
    """
    check_not_locked(file_path)
    make_history_backup(file_path)
    bak = make_backup(file_path)
    tmp = file_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, file_path)
    return bak


# ---------------------------------------------------------------------------
# Rolling numbered backup history (multi-step undo)
# ---------------------------------------------------------------------------

def _history_glob(file_path: str) -> str:
    return f"{os.path.abspath(file_path)}{HISTORY_SUFFIX}*"


def _history_index(path: str) -> int:
    m = re.search(re.escape(HISTORY_SUFFIX) + r"(\d+)$", path)
    return int(m.group(1)) if m else 0


def make_history_backup(file_path: str, keep: int = HISTORY_KEEP) -> str | None:
    """Copy the current file to the next numbered ``.vbakNNNN`` slot, pruning old ones."""
    file_path = os.path.abspath(file_path)
    if not os.path.isfile(file_path):
        return None
    existing = sorted(glob.glob(_history_glob(file_path)), key=_history_index)
    idx = (_history_index(existing[-1]) if existing else 0) + 1
    dst = f"{file_path}{HISTORY_SUFFIX}{idx:04d}"
    shutil.copy2(file_path, dst)
    for old in (existing + [dst])[:-keep]:
        try:
            os.remove(old)
        except OSError:
            pass
    return dst


NAMED_PREFIX = ".named-"


def create_named_backup(file_path: str, name: str) -> str:
    """Snapshot the current file under a chosen name (e.g. 'before-reroute').

    Unlike the automatic per-edit backups, this is a manual checkpoint you can
    return to later with restore_to. Returns the backup's full path.
    """
    file_path = os.path.abspath(file_path)
    if not os.path.isfile(file_path):
        raise BackupError(f"cannot back up {file_path}: file does not exist")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._-") or "snapshot"
    dst = f"{file_path}{NAMED_PREFIX}{safe}"
    shutil.copy2(file_path, dst)
    return dst


def list_backups(file_path: str) -> list[dict]:
    """All backups for a file: the last-edit ``.bak``, named snapshots, and the
    numbered history (newest history first). Each ``name`` is what
    :func:`restore_to` accepts.
    """
    file_path = os.path.abspath(file_path)
    out = []
    bak = backup_path(file_path)
    if os.path.isfile(bak):
        out.append({"name": os.path.basename(bak), "kind": "last_edit",
                    "size_bytes": os.path.getsize(bak)})
    for p in sorted(glob.glob(f"{file_path}{NAMED_PREFIX}*")):
        out.append({"name": os.path.basename(p), "kind": "named",
                    "size_bytes": os.path.getsize(p)})
    for p in sorted(glob.glob(_history_glob(file_path)), key=_history_index, reverse=True):
        out.append({"name": os.path.basename(p), "kind": "history",
                    "index": _history_index(p), "size_bytes": os.path.getsize(p)})
    return out


def restore_to(file_path: str, name: str) -> str:
    """Restore a specific backup (by its ``name`` from :func:`list_backups`)."""
    file_path = os.path.abspath(file_path)
    base = os.path.basename(file_path)
    if os.path.basename(name) != name or not name.startswith(base):
        raise BackupError(f"{name!r} is not a backup of {base}")
    src = os.path.join(os.path.dirname(file_path), name)
    if not os.path.isfile(src):
        raise BackupError(f"no such backup {name!r}")
    check_not_locked(file_path)
    shutil.copy2(src, file_path)
    return file_path
