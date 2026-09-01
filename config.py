"""Configuration and small shared helpers."""
from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Extensions we attempt to parse for symbols/imports.
CODE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".rb", ".rs"}

# Directories never worth mapping.
SKIP_DIRS = {
    ".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
    "target", ".next", ".tox", "vendor", "site-packages", ".mypy_cache",
    ".pytest_cache", "coverage", ".idea", ".vscode",
}


@dataclass
class Config:
    repo: Path
    out: Path
    # Episode clustering: commits within this many days of each other, on the
    # same feature, collapse into one episode.
    episode_gap_days: int = 14
    # Ignore commits touching more than this many files (renames, formatting
    # sweeps, vendored drops) -- they pollute co-change signal.
    max_files_per_commit: int = 40
    # How far back to mine history. None = all.
    since: str | None = None
    model: str = "claude-sonnet-4-6"
    max_modules: int = 400
    enable_llm: bool = True

    cache_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.repo = Path(self.repo).resolve()
        self.out = Path(self.out).resolve()
        self.cache_dir = self.out / ".cache"

    @classmethod
    def from_env(cls, repo: str = ".", out: str | None = None, **kw) -> "Config":
        repo_p = Path(repo).resolve()
        out_p = Path(out) if out else repo_p / "docs" / "map"
        return cls(repo=repo_p, out=out_p, **kw)


def sha1(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(p.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def run(args: list[str], cwd: Path, check: bool = False) -> str:
    """Run a command and return stdout. Git output can be huge; callers stream
    where it matters, but for our command shapes buffering is fine."""
    proc = subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True,
        errors="replace", env={**os.environ, "GIT_PAGER": "cat"},
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])} failed: {proc.stderr[:400]}")
    return proc.stdout


def should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def module_of(rel_path: str, depth: int = 2) -> str:
    """Map a file to its owning module. Two path segments is the sweet spot for
    most repos: deep enough to separate concerns, shallow enough that modules
    have more than one file in them."""
    parts = Path(rel_path).parts
    if len(parts) <= 1:
        return "(root)"
    return "/".join(parts[: min(depth, len(parts) - 1)])
