"""Orchestration: facts -> (sparse) LLM -> markdown map."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import gitmine, static, store
from .config import Config, module_of, run, sha1
from .llm import LLM


def _module_payload(name: str, files: list[static.FileFacts],
                    deps: set[str], entry_kinds: list[str]) -> dict:
    """What the model sees for a module. Signatures only -- never bodies.
    Capped so one enormous module can't blow up a batch."""
    sigs = []
    for f in sorted(files, key=lambda f: -len(f.symbols))[:12]:
        for s in [s for s in f.symbols if s.exported][:8]:
            sigs.append(s.signature[:120] + (f"  # {s.doc}" if s.doc else ""))
    return {
        "name": name,
        "files": [f.path for f in files][:15],
        "signatures": sigs[:40],
        "depends_on": sorted(deps)[:12],
        "entrypoints": sorted(set(entry_kinds))[:6],
    }


def _module_hash(files: list[static.FileFacts]) -> str:
    return sha1(*[f"{f.path}:{f.source_hash}" for f in sorted(files, key=lambda f: f.path)])[:16]


def build_modules(cfg: Config, facts: static.RepoFacts, llm: LLM) -> dict[str, dict]:
    modules = facts.modules()
    entry_by_mod: dict[str, list[str]] = defaultdict(list)
    for e in facts.entrypoints:
        entry_by_mod[module_of(e["file"])].append(e["kind"])

    ranked = sorted(modules.items(), key=lambda kv: -sum(f.loc for f in kv[1]))
    ranked = ranked[: cfg.max_modules]

    payloads, hashes, stale = [], {}, []
    for name, files in ranked:
        h = _module_hash(files)
        hashes[name] = h
        path = cfg.out / "modules" / f"{store.slug(name)}.md"
        if store.is_stale(path, h):
            stale.append(name)
        payloads.append(_module_payload(
            name, files, facts.dep_graph.get(name, set()), entry_by_mod.get(name, [])))

    summaries = llm.module_summaries(payloads)

    reverse_deps: dict[str, set[str]] = defaultdict(set)
    for src, tgts in facts.dep_graph.items():
        for t in tgts:
            reverse_deps[t].add(src)

    written = {}
    for name, files in ranked:
        s = summaries.get(name, {})
        fm = {
            "module": name,
            "source_hash": hashes[name],
            "last_built": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "role": s.get("role", "unknown"),
            "loc": sum(f.loc for f in files),
            "files": [f.path for f in files][:40],
            "depends_on": sorted(facts.dep_graph.get(name, set())),
            "used_by": sorted(reverse_deps.get(name, set())),
            "entrypoints": sorted(set(entry_by_mod.get(name, []))),
            "history": f"../history/{store.slug(name)}.md",
        }
        body = _render_module(name, files, facts, s, reverse_deps)
        store.write(cfg.out / "modules" / f"{store.slug(name)}.md", fm, body)
        written[name] = fm
    return written


def _render_module(name, files, facts, summary, reverse_deps) -> str:
    L = [f"# {name}", ""]
    if summary.get("purpose"):
        L += [summary["purpose"], ""]
    ep = [e for e in facts.entrypoints if module_of(e["file"]) == name]
    if ep:
        L += ["## Entry points", ""]
        for e in ep[:15]:
            L.append(f"- `{e['kind']}` {e['detail']} — `{e['file']}:{e['line']}`")
        L.append("")
    L += ["## Public surface", ""]
    for f in sorted(files, key=lambda f: -len(f.symbols))[:10]:
        exported = [s for s in f.symbols if s.exported][:10]
        if not exported:
            continue
        L.append(f"**`{f.path}`** ({f.loc} loc)")
        for s in exported:
            doc = f" — {s.doc}" if s.doc else ""
            L.append(f"  - `{s.signature[:110]}` `:{s.line}`{doc}")
        tests = facts.tests_for.get(f.path)
        if tests:
            L.append(f"  - _tests_: {', '.join(f'`{t}`' for t in tests[:3])}")
        L.append("")
    deps = sorted(facts.dep_graph.get(name, set()))
    if deps or reverse_deps.get(name):
        L += ["## Neighbours", ""]
        if deps:
            L.append("- depends on: " + ", ".join(f"[{d}](./{store.slug(d)}.md)" for d in deps[:12]))
        if reverse_deps.get(name):
            L.append("- used by: " + ", ".join(
                f"[{d}](./{store.slug(d)}.md)" for d in sorted(reverse_deps[name])[:12]))
        L.append("")
    return "\n".join(L)


# --- history ----------------------------------------------------------------

def build_history(cfg: Config, llm: LLM) -> dict[str, list[gitmine.Episode]]:
    commits = gitmine.log(cfg)
    if not commits:
        return {}
    episodes = gitmine.build_episodes(commits, cfg)
    cochange = gitmine.co_change(commits, cfg)

    for feature, eps in episodes.items():
        for ep in eps:
            payload = {
                "feature": feature,
                "period": f"{ep.start} to {ep.end}",
                "commit_subjects": ep.subjects[:40],
                "files": ep.files[:20],
                "authors": ep.authors[:6],
                "reverts": ep.reverted,
                "fix_commits": ep.fix_count,
                "linked_prs": ep.prs,
                "linked_issues": ep.issues,
            }
            res = llm.episode_summary(payload)
            ep.summary = res.get("summary", "")
            ep.evidence = res.get("confidence", "low")
            ep._decision = res.get("decision")  # type: ignore[attr-defined]

        fm = {
            "feature": feature,
            "episodes": len(eps),
            "first_change": eps[-1].start if eps else None,
            "last_change": eps[0].end if eps else None,
            "total_commits": sum(len(e.commits) for e in eps),
            "authors": sorted({a for e in eps for a in e.authors})[:10],
            "module": f"../modules/{store.slug(feature)}.md",
        }
        store.write(cfg.out / "history" / f"{store.slug(feature)}.md", fm,
                    _render_history(feature, eps, cochange))
    return episodes


def _render_history(feature, eps, cochange) -> str:
    L = [f"# History — {feature}", ""]
    decisions = [(e, getattr(e, "_decision", None)) for e in eps]
    decisions = [(e, d) for e, d in decisions if d]
    if decisions:
        L += ["## Decisions", ""]
        for e, d in decisions[:12]:
            refs = ", ".join(f"`{c}`" for c in e.commits[:3])
            prs = " " + " ".join(f"#{p}" for p in e.prs[:3]) if e.prs else ""
            L.append(f"- **{e.end}** — {d}  \n  _evidence: {refs}{prs} "
                     f"(confidence: {e.evidence})_")
        L.append("")

    contested = [e for e in eps if e.reverted]
    if contested:
        L += ["## Contested changes", "",
              "_Work that was reverted. These mark decisions that have already "
              "been argued about once._", ""]
        for e in contested[:8]:
            L.append(f"- {e.end}: reverted {', '.join(f'`{r}`' for r in e.reverted)} "
                     f"— {e.subjects[0][:100]}")
        L.append("")

    L += ["## Timeline", "", "_Newest first._", ""]
    for e in eps:
        span = e.start if e.start == e.end else f"{e.start} → {e.end}"
        L.append(f"### {span}  ·  {len(e.commits)} commits  ·  `{e.eid}`")
        if e.summary:
            L.append("")
            L.append(e.summary)
        L.append("")
        bits = [f"commits: {', '.join(f'`{c}`' for c in e.commits[:6])}"
                + (f" (+{len(e.commits)-6})" if len(e.commits) > 6 else "")]
        if e.prs:
            bits.append("PRs: " + ", ".join(f"#{p}" for p in e.prs[:6]))
        if e.issues:
            bits.append("issues: " + ", ".join(e.issues[:4]))
        if e.fix_count:
            bits.append(f"fix commits: {e.fix_count}")
        bits.append(f"by: {', '.join(e.authors[:4])}")
        L.append("<sub>" + " · ".join(bits) + "</sub>")
        L.append("")

    pairs = [(a, b, n) for a, b, n in cochange
             if module_of_safe(a) == feature or module_of_safe(b) == feature]
    if pairs:
        L += ["## Changes together with", "",
              "_Historical coupling. Often the real feature boundary._", ""]
        for a, b, n in pairs[:10]:
            L.append(f"- `{a}` ↔ `{b}` ({n}×)")
        L.append("")
    return "\n".join(L)


def module_of_safe(p: str) -> str:
    from .config import module_of as _m
    return _m(p)


# --- flows ------------------------------------------------------------------

def build_flows(cfg: Config, facts: static.RepoFacts, llm: LLM, limit: int = 25) -> list[dict]:
    """Walk the module dep graph from each entry point. A flow is a path, not a
    tree -- readers can hold a path in their head."""
    flows = []
    seen_paths = set()
    for e in facts.entrypoints[: limit * 3]:
        start = module_of(e["file"])
        path, cur, guard = [start], start, set([start])
        while len(path) < 6:
            nxt = sorted(facts.dep_graph.get(cur, set()) - guard)
            if not nxt:
                break
            cur = nxt[0]
            guard.add(cur)
            path.append(cur)
        if len(path) < 2:
            continue
        key = tuple(path)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        meta = llm.flow_summary({"entry": e["detail"], "kind": e["kind"], "path": path})
        flows.append({"entry": e, "path": path, **meta})
        if len(flows) >= limit:
            break

    for fl in flows:
        fid = store.slug(fl["name"])
        fm = {"flow": fl["name"], "kind": fl["entry"]["kind"],
              "entry": f"{fl['entry']['file']}:{fl['entry']['line']}",
              "modules": fl["path"]}
        body = _render_flow(fl)
        store.write(cfg.out / "flows" / f"{fid}.md", fm, body)
    return flows


def _render_flow(fl) -> str:
    steps = fl["path"]
    lines = [f"# {fl['name']}", ""]
    if fl.get("description"):
        lines += [fl["description"], ""]
    lines += [f"Entry: `{fl['entry']['kind']}` `{fl['entry']['file']}:{fl['entry']['line']}`", ""]
    lines += ["```mermaid", "flowchart LR"]
    for i, s in enumerate(steps):
        lines.append(f'  n{i}["{s}"]')
    for i in range(len(steps) - 1):
        lines.append(f"  n{i} --> n{i+1}")
    lines += ["```", ""]
    lines += ["## Steps", ""]
    for i, s in enumerate(steps, 1):
        lines.append(f"{i}. [`{s}`](../modules/{store.slug(s)}.md)")
    lines.append("")
    return "\n".join(lines)


# --- index ------------------------------------------------------------------

def build_index(cfg: Config, modules: dict, facts: static.RepoFacts,
                episodes: dict, flows: list, llm: LLM) -> None:
    head = run(["git", "rev-parse", "--short", "HEAD"], cfg.repo).strip()
    fm = {
        "repo": cfg.repo.name,
        "commit": head or None,
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "modules": len(modules),
        "files": len(facts.files),
        "flows": len(flows),
        "llm": llm.stats(),
    }
    L = [f"# Map — {cfg.repo.name}", "",
         f"{len(facts.files)} source files · {len(modules)} modules · "
         f"{len(facts.entrypoints)} entry points", ""]

    if flows:
        L += ["## Flows", ""]
        for fl in flows[:20]:
            L.append(f"- [{fl['name']}](./flows/{store.slug(fl['name'])}.md) "
                     f"— `{fl['entry']['kind']}` {fl['entry']['detail'][:60]}")
        L.append("")

    L += ["## Modules", "", "| module | role | loc | history |", "|---|---|---|---|"]
    for name, m in sorted(modules.items(), key=lambda kv: -kv[1]["loc"]):
        n_ep = len(episodes.get(name, []))
        hist = (f"[{n_ep} episode{'s' if n_ep != 1 else ''}]"
                f"(./history/{store.slug(name)}.md)" if episodes.get(name) else "—")
        L.append(f"| [{name}](./modules/{store.slug(name)}.md) | {m['role']} "
                 f"| {m['loc']} | {hist} |")
    L.append("")
    store.write(cfg.out / "INDEX.md", fm, "\n".join(L))


def build_all(cfg: Config) -> dict:
    llm = LLM(cfg)
    facts = static.collect(cfg)
    modules = build_modules(cfg, facts, llm)
    episodes = build_history(cfg, llm)
    flows = build_flows(cfg, facts, llm)
    build_index(cfg, modules, facts, episodes, flows, llm)
    return {"modules": len(modules), "files": len(facts.files),
            "features_with_history": len(episodes), "flows": len(flows),
            **llm.stats()}
