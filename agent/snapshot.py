"""Cheap stdlib file snapshots: what changed in the working folder during a turn.

A diff only shows that files changed while an agent ran; it does NOT prove which agent did it.
Never restores or deletes anything.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".idea", ".vscode",
             ".mypy_cache", ".pytest_cache", ".tox", "dist", "build", "target", ".next", ".cache"}


@dataclass
class Snapshot:
    files: Dict[str, Tuple[int, int]] = field(default_factory=dict)  # relpath -> (size, mtime_ns)
    complete: bool = True
    notes: List[str] = field(default_factory=list)


@dataclass
class FileDiff:
    added: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    complete: bool = True
    notes: List[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.modified or self.deleted)

    def summary(self, limit: int = 8) -> str:
        if self.empty:
            s = "no file changes observed"
        else:
            def part(sym, items):
                shown = ", ".join(items[:limit]) + (f", ... (+{len(items) - limit})" if len(items) > limit else "")
                return f"{sym}{len(items)} [{shown}]" if items else ""
            s = "observed file changes: " + " ".join(
                p for p in (part("+", self.added), part("~", self.modified), part("-", self.deleted)) if p)
        if not self.complete:
            s += " (INCOMPLETE scan: " + "; ".join(self.notes) + ")"
        return s + " [snapshot only; may not be caused by this agent]"

    def to_dict(self) -> dict:
        return {"added": self.added, "modified": self.modified, "deleted": self.deleted,
                "complete": self.complete, "notes": self.notes}


def take_snapshot(root: str, max_files: int = 20000, max_seconds: float = 5.0,
                  skip_dirs: Optional[set] = None) -> Snapshot:
    skip = SKIP_DIRS if skip_dirs is None else skip_dirs
    snap = Snapshot()
    start = time.monotonic()
    stack = [root]
    denied = 0
    while stack:
        cur = stack.pop()
        try:
            it = os.scandir(cur)
        except OSError:
            denied += 1
            continue
        with it:
            for e in it:
                if len(snap.files) >= max_files:
                    snap.complete = False
                    snap.notes.append(f"file limit {max_files} reached")
                    return snap
                if time.monotonic() - start > max_seconds:
                    snap.complete = False
                    snap.notes.append(f"time limit {max_seconds}s reached")
                    return snap
                try:
                    if e.is_symlink():
                        continue  # never follow links out of the tree
                    if e.is_dir(follow_symlinks=False):
                        if e.name not in skip:
                            stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        snap.files[os.path.relpath(e.path, root)] = (st.st_size, st.st_mtime_ns)
                except OSError:
                    denied += 1
    if denied:
        snap.complete = False
        snap.notes.append(f"{denied} entries unreadable")
    return snap


def diff_snapshots(a: Snapshot, b: Snapshot) -> FileDiff:
    d = FileDiff()
    for p, v in b.files.items():
        if p not in a.files:
            d.added.append(p)
        elif a.files[p] != v:
            d.modified.append(p)
    d.deleted = [p for p in a.files if p not in b.files]
    for lst in (d.added, d.modified, d.deleted):
        lst.sort()
    d.complete = a.complete and b.complete
    d.notes = list(dict.fromkeys(a.notes + b.notes))
    return d
