"""The map on disk: markdown with YAML frontmatter, one file per module.

Human-readable, git-diffable, greppable, and hand-editable. The generator owns
everything except the `## Notes` block, which it must never overwrite -- that's
where a reader's own understanding accumulates, and losing it once would kill
trust in the tool permanently.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

NOTES_HEADING = "## Notes"
GEN_MARK = "<!-- generated: do not edit above this line -->"
FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)


def slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-").lower() or "root"


def read(path: Path) -> tuple[dict, str]:
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    m = FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, text[m.end():]


def existing_notes(path: Path) -> str:
    """Pull the hand-written tail out of a previous version of the file."""
    _, body = read(path)
    idx = body.find(NOTES_HEADING)
    if idx == -1:
        return ""
    return body[idx + len(NOTES_HEADING):].strip()


def write(path: Path, frontmatter: dict, body: str, preserve_notes: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    notes = existing_notes(path) if preserve_notes else ""
    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True,
                        default_flow_style=False).strip()
    parts = ["---", fm, "---", "", body.strip(), "", GEN_MARK, "", NOTES_HEADING, ""]
    parts.append(notes if notes else "_Your own observations go here. "
                                     "This section is never overwritten._")
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def is_stale(path: Path, source_hash: str) -> bool:
    fm, _ = read(path)
    return fm.get("source_hash") != source_hash


def load_all(root: Path, subdir: str) -> list[tuple[Path, dict, str]]:
    out = []
    d = root / subdir
    if not d.exists():
        return out
    for p in sorted(d.glob("*.md")):
        fm, body = read(p)
        out.append((p, fm, body))
    return out
