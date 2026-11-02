"""Bounded file-state observations; never proof of execution history."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat

MAX_BYTES = 1_000_000


def observe(workspace: str, relative_path: str) -> dict:
    if not all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")):
        return {"state": "unsupported"}
    parts = Path(relative_path).parts
    if not parts or Path(relative_path).is_absolute() or any(p in {".", ".."} for p in parts):
        return {"state": "unsupported"}
    descriptor = None
    try:
        descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts[:-1]:
            next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        target = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        try:
            before = os.fstat(target)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_BYTES:
                return {"state": "unsupported"}
            with os.fdopen(os.dup(target), "rb") as stream:
                data = stream.read(MAX_BYTES + 1)
            after = os.fstat(target)
            current = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
                return {"state": "unstable"}
            if len(data) > MAX_BYTES or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                return {"state": "unstable"}
            return {"state": "present", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        finally:
            os.close(target)
    except FileNotFoundError:
        return {"state": "absent"}
    except OSError:
        return {"state": "unreadable"}
    finally:
        if descriptor is not None:
            os.close(descriptor)


def describe_creation(action_name: str, arguments: dict, workspace: str) -> dict | None:
    if action_name != "patch" or not isinstance(arguments.get("diff"), str):
        return None
    if len(arguments["diff"]) > MAX_BYTES:
        return None
    lines = arguments["diff"].splitlines()
    # No extended headers, relocation, binary data or multiple files.
    if len(lines) < 4 or lines[0] != "--- /dev/null" or not lines[1].startswith("+++ b/"):
        return None
    match = re.fullmatch(r"@@ -0,0 \+1,(\d+) @@", lines[2])
    if match is None or not all(line.startswith("+") for line in lines[3:]):
        return None
    if int(match[1]) != len(lines[3:]) or not lines[3:]:
        return None
    relative_path = lines[1][6:]
    if "\\" in relative_path or not relative_path or relative_path != Path(relative_path).as_posix():
        return None
    data = ("\n".join(line[1:] for line in lines[3:]) + "\n").encode("utf-8")
    if len(data) > MAX_BYTES or observe(workspace, relative_path) != {"state": "absent"}:
        return None
    return {"kind": "file_creation_v1", "path": relative_path,
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
