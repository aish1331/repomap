"""Layer 1: deterministic facts. No LLM calls happen in this module.

Everything here is cheap, repeatable, and forms the skeleton of the map. The
LLM layer only ever sees the *output* of this file, never raw source, which is
where the token savings come from.
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .config import CODE_EXTS, Config, module_of, run, sha1, should_skip


@dataclass
class Symbol:
    name: str
    kind: str          # function | class | method | const
    line: int
    signature: str
    doc: str = ""      # first line of docstring only
    exported: bool = True


@dataclass
class FileFacts:
    path: str                       # repo-relative
    lang: str
    loc: int
    source_hash: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)   # raw import targets
    is_test: bool = False


@dataclass
class RepoFacts:
    files: dict[str, FileFacts] = field(default_factory=dict)
    # module -> set(module) edges, resolved from file imports
    dep_graph: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    entrypoints: list[dict] = field(default_factory=list)
    tests_for: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def modules(self) -> dict[str, list[FileFacts]]:
        out: dict[str, list[FileFacts]] = defaultdict(list)
        for f in self.files.values():
            out[module_of(f.path)].append(f)
        return out


LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".go": "go",
    ".java": "java", ".rb": "ruby", ".rs": "rust",
}

TEST_HINT = re.compile(r"(^|/)(tests?|spec|__tests__)/|(_test|\.test|\.spec|_spec)\.", re.I)

# --- entry point heuristics -------------------------------------------------
ENTRY_PATTERNS = [
    ("http-route", re.compile(
        r"""@(?:app|router|bp|blueprint)\.(get|post|put|patch|delete|route)\(\s*["']([^"']+)""", re.I)),
    ("http-route", re.compile(
        r"""(?:app|router)\.(get|post|put|patch|delete)\(\s*["']([^"']+)""")),
    ("http-route", re.compile(
        r"""@(?:Get|Post|Put|Patch|Delete|RequestMapping|GetMapping|PostMapping)\(\s*["']?([^"')]+)""")),
    ("cli", re.compile(r"""@(?:click|app)\.command\(|argparse\.ArgumentParser\(|cobra\.Command\{""")),
    ("job", re.compile(r"""@(?:celery|shared_task|app)\.task\b|@scheduled\b|cron\.schedule\(""", re.I)),
    ("event", re.compile(
        r"""(?:consumer|subscriber)\.(?:subscribe|on)\(\s*["']([^"']+)|@(?:KafkaListener|EventHandler)\b""")),
    ("main", re.compile(r"""^if\s+__name__\s*==\s*["']__main__["']|^func\s+main\(\)""", re.M)),
]


def _py_signature(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = []
        a = node.args
        for arg in list(a.posonlyargs) + list(a.args):
            args.append(arg.arg + (f": {ast.unparse(arg.annotation)}" if arg.annotation else ""))
        if a.vararg:
            args.append("*" + a.vararg.arg)
        for arg in a.kwonlyargs:
            args.append(arg.arg + (f": {ast.unparse(arg.annotation)}" if arg.annotation else ""))
        if a.kwarg:
            args.append("**" + a.kwarg.arg)
        ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({', '.join(args)}){ret}"
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        return f"class {node.name}({bases})" if bases else f"class {node.name}"
    return ""


def _parse_python(text: str, facts: FileFacts) -> None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                facts.imports += [a.name for a in node.names]
            else:
                mod = ("." * (node.level or 0)) + (node.module or "")
                facts.imports.append(mod)
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = (ast.get_docstring(node) or "").strip().split("\n")[0][:160]
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            facts.symbols.append(Symbol(
                name=node.name, kind=kind, line=node.lineno,
                signature=_py_signature(node), doc=doc,
                exported=not node.name.startswith("_"),
            ))
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                            and not sub.name.startswith("_"):
                        facts.symbols.append(Symbol(
                            name=f"{node.name}.{sub.name}", kind="method",
                            line=sub.lineno, signature=_py_signature(sub),
                            doc=(ast.get_docstring(sub) or "").strip().split("\n")[0][:160],
                        ))


JS_IMPORT = re.compile(r"""(?:import[^"']*from\s*|require\(\s*)["']([^"']+)["']""")
JS_SYMBOL = re.compile(
    r"""^\s*export\s+(?:default\s+)?(?:(async\s+)?function\s+(\w+)\s*(\([^)]*\))"""
    r"""|class\s+(\w+)|const\s+(\w+)\s*=\s*(?:(async\s*)?(\([^)]*\)|\w+)\s*=>)?)""",
    re.M)
GENERIC_IMPORT = re.compile(
    r"""^\s*(?:import\s+(?:"([^"]+)"|([\w.]+))|use\s+([\w:]+)|require\s+["']([^"']+))""", re.M)
GENERIC_SYMBOL = re.compile(
    r"""^\s*(?:(?:public|private|protected)\s+)?(?:static\s+)?"""
    r"""(?:func|function|def|fn|class|interface|struct|type)\s+(\w+)([^\n{;]*)""", re.M)


def _parse_js(text: str, facts: FileFacts) -> None:
    facts.imports += JS_IMPORT.findall(text)
    for m in JS_SYMBOL.finditer(text):
        _, fn, args, cls, const, _, _ = m.groups()
        name = fn or cls or const
        if not name:
            continue
        line = text[: m.start()].count("\n") + 1
        kind = "class" if cls else "function"
        sig = f"{name}{args}" if args else name
        facts.symbols.append(Symbol(name=name, kind=kind, line=line, signature=sig))


def _parse_generic(text: str, facts: FileFacts) -> None:
    for groups in GENERIC_IMPORT.findall(text):
        target = next((g for g in groups if g), None)
        if target:
            facts.imports.append(target)
    for m in GENERIC_SYMBOL.finditer(text):
        name, rest = m.group(1), (m.group(2) or "").strip()
        line = text[: m.start()].count("\n") + 1
        facts.symbols.append(Symbol(
            name=name, kind="function", line=line,
            signature=f"{name}{rest}"[:200],
        ))


def parse_file(path: Path, rel: str) -> FileFacts | None:
    ext = path.suffix.lower()
    if ext not in CODE_EXTS:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(text) > 400_000:      # generated/bundled file
        return None
    facts = FileFacts(
        path=rel, lang=LANG_BY_EXT.get(ext, "unknown"),
        loc=text.count("\n") + 1, source_hash=sha1(text)[:16],
        is_test=bool(TEST_HINT.search(rel)),
    )
    if ext == ".py":
        _parse_python(text, facts)
    elif ext in {".js", ".jsx", ".ts", ".tsx"}:
        _parse_js(text, facts)
    else:
        _parse_generic(text, facts)

    for kind, pat in ENTRY_PATTERNS:
        for m in pat.finditer(text):
            groups = [g for g in m.groups() if g] if m.groups() else []
            facts_entry = {
                "kind": kind, "file": rel,
                "line": text[: m.start()].count("\n") + 1,
                "detail": " ".join(groups)[:120] or kind,
            }
            _ENTRY_SINK.append(facts_entry)
    return facts


_ENTRY_SINK: list[dict] = []


def _tracked_files(cfg: Config) -> list[str]:
    out = run(["git", "ls-files"], cfg.repo)
    if out.strip():
        return [l for l in out.splitlines() if l]
    # Not a git repo, or empty index: walk the tree instead.
    return [
        str(p.relative_to(cfg.repo))
        for p in cfg.repo.rglob("*")
        if p.is_file() and not should_skip(p.relative_to(cfg.repo))
    ]


def _resolve_import(raw: str, src_file: str, by_stem: dict[str, list[str]]) -> str | None:
    """Best-effort mapping from an import string to a repo file. Deliberately
    conservative -- a wrong edge is worse than a missing one."""
    if raw.startswith("."):
        base = Path(src_file).parent
        target = (base / raw.lstrip(".").replace(".", "/")).as_posix() if len(raw) > 1 else base.as_posix()
        stem = Path(target).name or Path(src_file).parent.name
    else:
        stem = raw.replace(":", "/").replace(".", "/").rstrip("/").split("/")[-1]
    if not stem:
        return None
    candidates = by_stem.get(stem)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Prefer the candidate sharing the longest path prefix with the importer.
    def shared(c: str) -> int:
        a, b = Path(c).parts, Path(src_file).parts
        n = 0
        for x, y in zip(a, b):
            if x != y:
                break
            n += 1
        return n
    return max(candidates, key=shared)


def collect(cfg: Config) -> RepoFacts:
    _ENTRY_SINK.clear()
    repo_facts = RepoFacts()
    for rel in _tracked_files(cfg):
        p = cfg.repo / rel
        if should_skip(Path(rel)) or not p.is_file():
            continue
        ff = parse_file(p, rel)
        if ff:
            repo_facts.files[rel] = ff

    by_stem: dict[str, list[str]] = defaultdict(list)
    for rel in repo_facts.files:
        by_stem[Path(rel).stem].append(rel)
        by_stem[Path(rel).parent.name].append(rel)

    for rel, ff in repo_facts.files.items():
        src_mod = module_of(rel)
        for raw in ff.imports:
            tgt_file = _resolve_import(raw, rel, by_stem)
            if not tgt_file:
                continue
            tgt_mod = module_of(tgt_file)
            if tgt_mod != src_mod:
                repo_facts.dep_graph[src_mod].add(tgt_mod)
        if ff.is_test:
            stem = re.sub(r"(^test_|_test$|\.test$|\.spec$|_spec$)", "", Path(rel).stem)
            for cand in by_stem.get(stem, []):
                if cand != rel and not repo_facts.files[cand].is_test:
                    repo_facts.tests_for[cand].append(rel)

    repo_facts.entrypoints = list(_ENTRY_SINK)
    return repo_facts
