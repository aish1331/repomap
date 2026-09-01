"""Layer 1b: history facts mined from git.

Episodes are the unit of compression. A feature's history is hundreds of
commits; grouped into episodes it's a dozen entries. Episodes are immutable
once written, so the LLM summarises each one exactly once, ever.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from .config import Config, module_of, run, sha1

REC = "\x1e"   # record separator
FLD = "\x1f"   # field separator

PR_RE = re.compile(r"(?:#|pull request )(\d+)")
REVERT_RE = re.compile(r'^Revert\s+"(.+?)"', re.M)
# (?!-) keeps "fixed-window rate limiter" from being read as a bug fix.
FIX_RE = re.compile(r"\b(fix(e[sd])?|bugfix|hotfix|regression)\b(?!-)", re.I)
ISSUE_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")   # JIRA-style


@dataclass
class Commit:
    sha: str
    short: str
    author: str
    date: str            # ISO
    subject: str
    body: str
    files: list[str] = field(default_factory=list)

    @property
    def dt(self) -> datetime:
        return datetime.fromisoformat(self.date)

    @property
    def prs(self) -> list[str]:
        return PR_RE.findall(self.subject + "\n" + self.body)

    @property
    def issues(self) -> list[str]:
        return ISSUE_RE.findall(self.subject + "\n" + self.body)

    @property
    def is_revert(self) -> bool:
        return bool(REVERT_RE.search(self.subject))

    @property
    def is_fix(self) -> bool:
        return bool(FIX_RE.search(self.subject))


@dataclass
class Episode:
    feature: str
    eid: str                       # stable hash of member SHAs
    start: str
    end: str
    commits: list[str]             # short shas
    authors: list[str]
    files: list[str]
    prs: list[str]
    issues: list[str]
    subjects: list[str]
    reverted: list[str] = field(default_factory=list)
    fix_count: int = 0
    summary: str = ""              # filled by the LLM layer
    evidence: str = "commits"      # commits | pr-body | mixed

    def to_dict(self) -> dict:
        return asdict(self)


def log(cfg: Config, extra: list[str] | None = None) -> list[Commit]:
    """One git call for the whole history. --numstat would give churn but
    doubles output size; name-only is enough for clustering."""
    # The record separator must LEAD the format: with --name-only, git prints
    # the file list *after* the pretty output, so a trailing separator would
    # attach every commit's files to the next commit.
    fmt = REC + FLD.join(["%H", "%h", "%an", "%aI", "%s", "%b"])
    args = ["git", "log", "--no-merges", "--name-only", f"--pretty=format:{fmt}"]
    if cfg.since:
        args.append(f"--since={cfg.since}")
    args += extra or []
    raw = run(args, cfg.repo)
    commits: list[Commit] = []
    for chunk in raw.split(REC):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        parts = chunk.split(FLD)
        if len(parts) < 6:
            continue
        sha, short, author, date, subject, tail = parts[:6]
        body_lines, files = [], []
        seen_blank = False
        for line in tail.split("\n"):
            if not line.strip():
                seen_blank = True
                continue
            # Filenames appear after the body, and always look like paths.
            if seen_blank and ("/" in line or "." in line) and " " not in line.strip():
                files.append(line.strip())
            else:
                body_lines.append(line)
        commits.append(Commit(
            sha=sha.lstrip("\n"), short=short, author=author, date=date,
            subject=subject, body="\n".join(body_lines).strip()[:2000],
            files=files,
        ))
    return commits


def merge_commits(cfg: Config) -> dict[str, str]:
    """SHA -> merge commit subject, so squash-merge repos still yield PR refs."""
    fmt = FLD.join(["%h", "%s", "%b"]) + REC
    raw = run(["git", "log", "--merges", f"--pretty=format:{fmt}"], cfg.repo)
    out = {}
    for chunk in raw.split(REC):
        parts = chunk.strip("\n").split(FLD)
        if len(parts) >= 2:
            out[parts[0]] = (parts[1] + " " + (parts[2] if len(parts) > 2 else "")).strip()
    return out


def feature_of(commit: Commit, cfg: Config) -> str | None:
    """A commit belongs to the module most of its files live in. Sweeping
    commits belong to nothing -- excluding them keeps history readable."""
    files = [f for f in commit.files if not f.startswith(".")]
    if not files or len(files) > cfg.max_files_per_commit:
        return None
    counts = Counter(module_of(f) for f in files)
    top, n = counts.most_common(1)[0]
    # Require a real plurality, else the commit is cross-cutting.
    return top if n >= max(1, len(files) * 0.5) else None


def co_change(commits: list[Commit], cfg: Config, min_pairs: int = 3) -> list[tuple[str, str, int]]:
    """Files that change together define the real feature boundary, which is
    often not the directory boundary."""
    pairs: Counter = Counter()
    for c in commits:
        files = sorted({f for f in c.files})[: cfg.max_files_per_commit]
        if len(files) < 2 or len(files) > 12:
            continue
        for i, a in enumerate(files):
            for b in files[i + 1:]:
                pairs[(a, b)] += 1
    return [(a, b, n) for (a, b), n in pairs.most_common(200) if n >= min_pairs]


def build_episodes(commits: list[Commit], cfg: Config) -> dict[str, list[Episode]]:
    """Group each feature's commits into time-contiguous episodes."""
    by_feature: dict[str, list[Commit]] = defaultdict(list)
    for c in commits:
        f = feature_of(c, cfg)
        if f:
            by_feature[f].append(c)

    gap = timedelta(days=cfg.episode_gap_days)
    merges = merge_commits(cfg)
    revert_targets = _revert_index(commits)
    out: dict[str, list[Episode]] = {}

    for feature, cs in by_feature.items():
        cs.sort(key=lambda c: c.dt)
        groups: list[list[Commit]] = []
        for c in cs:
            if groups and c.dt - groups[-1][-1].dt <= gap:
                groups[-1].append(c)
            else:
                groups.append([c])
        episodes = []
        for g in groups:
            prs, issues = [], []
            for c in g:
                prs += c.prs
                issues += c.issues
                if c.short in merges:
                    prs += PR_RE.findall(merges[c.short])
            files = sorted({f for c in g for f in c.files})
            episodes.append(Episode(
                feature=feature,
                eid=sha1(feature, *[c.sha for c in g])[:12],
                start=g[0].date[:10], end=g[-1].date[:10],
                commits=[c.short for c in g],
                authors=sorted({c.author for c in g}),
                files=files[:40],
                prs=sorted(set(prs), key=int)[:12],
                issues=sorted(set(issues))[:8],
                subjects=[c.subject for c in g][:60],
                reverted=[revert_targets[c.subject] for c in g
                          if c.subject in revert_targets],
                fix_count=sum(1 for c in g if c.is_fix),
            ))
        episodes.reverse()   # newest first, matching how the file reads
        out[feature] = episodes
    return out


def _revert_index(commits: list[Commit]) -> dict[str, str]:
    """Map a revert commit's subject to the short SHA it undid. Reverts mark
    decisions that were contested -- the highest-signal events in a history."""
    by_subject = {c.subject: c.short for c in commits}
    idx = {}
    for c in commits:
        m = REVERT_RE.search(c.subject)
        if m and m.group(1) in by_subject:
            idx[c.subject] = by_subject[m.group(1)]
    return idx


# --- provenance for a specific diff -----------------------------------------

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)
FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.M)


def changed_ranges(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    current = None
    for line in diff_text.splitlines():
        fm = FILE_RE.match(line)
        if fm:
            current = fm.group(1)
            continue
        hm = HUNK_RE.match(line)
        if hm and current:
            start = int(hm.group(1))
            length = int(hm.group(2) or 1)
            if length:
                out[current].append((start, start + length - 1))
    return dict(out)


def line_history(cfg: Config, path: str, start: int, end: int, limit: int = 12) -> list[str]:
    """`git log -L` gives the commits that actually touched these lines. This is
    the query that makes review useful: what happened here before?"""
    raw = run([
        "git", "log", f"-L{start},{end}:{path}", "--no-patch",
        f"-n{limit}", "--pretty=format:%h%x1f%aI%x1f%an%x1f%s",
    ], cfg.repo)
    return [l for l in raw.splitlines() if l.strip()]
