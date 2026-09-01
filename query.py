"""Read side of the map. This is what a reviewer (or a review agent) calls.

The important one is explain_diff: given a PR, return the surrounding context
and the prior history of exactly the lines being touched. "This code was
changed three times before, here's why" is the whole point of the tool.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import gitmine, store
from .config import Config, module_of, run


def _mod_path(cfg: Config, name: str) -> Path:
    return cfg.out / "modules" / f"{store.slug(name)}.md"


def overview(cfg: Config) -> str:
    p = cfg.out / "INDEX.md"
    return p.read_text() if p.exists() else "No map built yet. Run: repomap build"


def module(cfg: Config, name: str) -> str:
    p = _mod_path(cfg, name)
    if p.exists():
        return p.read_text()
    # Accept a file path as well as a module name -- reviewers think in files.
    p2 = _mod_path(cfg, module_of(name))
    return p2.read_text() if p2.exists() else f"No entry for '{name}'."


def history(cfg: Config, name: str) -> str:
    p = cfg.out / "history" / f"{store.slug(module_of(name) if '/' in name and name.endswith(tuple('.py .js .ts .go .java .rb .rs'.split())) else name)}.md"
    return p.read_text() if p.exists() else f"No history for '{name}'."


def flow(cfg: Config, name: str) -> str:
    p = cfg.out / "flows" / f"{store.slug(name)}.md"
    if p.exists():
        return p.read_text()
    hits = [q for q in (cfg.out / "flows").glob("*.md")
            if name.lower() in q.stem.replace("-", " ")]
    return hits[0].read_text() if hits else f"No flow named '{name}'."


def search(cfg: Config, term: str, limit: int = 20) -> list[str]:
    """grep the map. Deliberately simple: the map is small and plain text, so
    ripgrep-style matching beats any index we could maintain."""
    out = []
    pat = re.compile(re.escape(term), re.I)
    for p in sorted(cfg.out.rglob("*.md")):
        for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if pat.search(line):
                rel = p.relative_to(cfg.out)
                out.append(f"{rel}:{i}: {line.strip()[:160]}")
                if len(out) >= limit:
                    return out
    return out


def callers(cfg: Config, symbol: str, limit: int = 30) -> list[str]:
    """Ground truth from the repo, not from the map -- cheaper and never stale."""
    raw = run(["git", "grep", "-n", "-w", symbol], cfg.repo)
    return [l for l in raw.splitlines() if l][:limit]


def introduced_by(cfg: Config, snippet: str, limit: int = 5) -> list[str]:
    """Pickaxe: which commit first added this string?"""
    raw = run(["git", "log", "-S", snippet, f"-n{limit}",
               "--pretty=format:%h %aI %an — %s"], cfg.repo)
    return [l for l in raw.splitlines() if l]


def explain_diff(cfg: Config, diff_text: str | None = None,
                 base: str = "origin/main") -> str:
    """The review-time entry point.

    Returns: which modules the PR touches, what those modules are for, and the
    prior episodes/commits that touched the same lines.
    """
    if diff_text is None:
        diff_text = run(["git", "diff", f"{base}...HEAD", "-U0"], cfg.repo)
    if not diff_text.strip():
        return "Empty diff."

    ranges = gitmine.changed_ranges(diff_text)
    # The map lives in the repo, so a PR that rebuilt it would otherwise drown
    # the real changes. Same for lockfiles and other generated noise.
    try:
        map_rel = cfg.out.relative_to(cfg.repo).as_posix() + "/"
    except ValueError:
        map_rel = None
    NOISE = re.compile(r"(lock\.json$|\.lock$|\.min\.|\.snap$|/dist/|/build/)")
    ranges = {
        f: v for f, v in ranges.items()
        if not (map_rel and f.startswith(map_rel)) and not NOISE.search(f)
    }
    if not ranges:
        return "No reviewable source changes found in the diff."

    touched_modules = sorted({module_of(f) for f in ranges})
    L = ["# PR context", "",
         f"Touches {len(ranges)} files across {len(touched_modules)} modules.", ""]

    L += ["## Modules involved", ""]
    for m in touched_modules:
        p = _mod_path(cfg, m)
        purpose = ""
        if p.exists():
            fm, body = store.read(p)
            for line in body.splitlines():
                if line.strip() and not line.startswith("#"):
                    purpose = line.strip()
                    break
            L.append(f"### {m}  ({fm.get('role', '?')})")
            if purpose:
                L.append(purpose)
            if fm.get("used_by"):
                L.append(f"- **Blast radius**: used by {', '.join(fm['used_by'][:8])}")
            if fm.get("entrypoints"):
                L.append(f"- Reachable from: {', '.join(fm['entrypoints'])}")
            notes = store.existing_notes(p)
            if notes and "never overwritten" not in notes:
                L.append(f"- **Your notes**: {notes.splitlines()[0][:200]}")
        else:
            L.append(f"### {m}\n_Not in the map yet._")
        L.append("")

    L += ["## Prior history of the exact lines changed", "",
          "_If a line has been fixed repeatedly, that's where the invariants are._", ""]
    for path, spans in list(ranges.items())[:25]:
        entries = []
        for start, end in spans[:6]:
            entries += gitmine.line_history(cfg, path, start, end, limit=6)
        if not entries:
            continue
        seen, uniq = set(), []
        for e in entries:
            sha = e.split("\x1f")[0]
            if sha not in seen:
                seen.add(sha)
                uniq.append(e)
        L.append(f"**`{path}`**")
        for e in uniq[:8]:
            parts = e.split("\x1f")
            if len(parts) >= 4:
                sha, date, author, subj = parts[:4]
                flag = " ⚠️" if gitmine.FIX_RE.search(subj) else ""
                L.append(f"- `{sha}` {date[:10]} {author} — {subj[:100]}{flag}")
        fixes = sum(1 for e in uniq if gitmine.FIX_RE.search(e.split("\x1f")[-1]))
        if fixes >= 2:
            L.append(f"- **{fixes} of the last {len(uniq)} changes here were fixes.** "
                     f"Read them before approving.")
        L.append("")

    hist_links = [f"- [{m}](./history/{store.slug(m)}.md)" for m in touched_modules
                  if (cfg.out / "history" / f"{store.slug(m)}.md").exists()]
    if hist_links:
        L += ["## Full feature histories", ""] + hist_links + [""]
    return "\n".join(L)
