# repomap

Builds a searchable, git-aware mental map of a codebase so you can walk into an
unfamiliar PR without reconstructing the whole subsystem in your head first.

The map is plain markdown checked into the repo. It survives interruptions,
because your understanding lives on disk instead of in working memory.

## Design

Three layers, in strict order:

| Layer | Source | Token cost |
|---|---|---|
| Facts | `git ls-files`, AST/regex parsing, import graph, `git log` | zero |
| Prose | LLM, fed **signatures and commit subjects only** — never source bodies | small, cached |
| Query | `git log -L`, `git log -S`, grep against the working tree | zero |

Token control comes from three things: the model never sees file contents; every
result is cached under a hash of its exact input, so an unchanged module is never
re-summarised; and history is compressed into *episodes* (time-contiguous
clusters of commits on one feature), which are immutable once written and so cost
tokens exactly once, ever.

On the demo repo a cold build was 9 API calls; the rebuild was 0.

## Layout

```
docs/map/
  INDEX.md                # modules, roles, flows, sizes
  modules/<module>.md     # purpose, public surface, entry points, blast radius
  history/<feature>.md    # decisions, contested changes, episode timeline
  flows/<flow>.md         # entry point -> module chain, with a mermaid diagram
  .cache/                 # content-hash keyed LLM results (gitignore this)
```

Every file is YAML frontmatter (machine-readable) plus markdown (human-readable),
ending in a `## Notes` section **the generator never overwrites**. Notes you
write there are surfaced back to you during review.

## Usage

```bash
export ANTHROPIC_API_KEY=...        # omit, or pass --no-llm, for a facts-only map
pip install -e .

repomap build                       # or: repomap build --since '2 years ago'
repomap pr --base origin/main       # ← the review-time command
repomap module core/ratelimit.py
repomap history billing
repomap search "idempotency"
repomap introduced "TokenBucket"    # git pickaxe: which commit added this?
repomap serve                       # MCP server on stdio
```

`repomap pr` is the one that solves the original problem. For the diff under
review it prints: which modules are touched and what they're for, their blast
radius, your own saved notes on them, and **the prior commits that touched the
exact lines being changed** — flagging when several of them were bug fixes.

## MCP

`repomap serve` exposes `map_overview`, `map_module`, `map_history`, `map_flow`,
`map_search`, `map_callers`, `map_introduced_by`, `map_explain_diff`,
`map_rebuild` over stdio JSON-RPC, no dependencies. Serving the map rather than
dumping it is the point: an agent pulls the three nodes it needs instead of
loading the whole map into context.

```json
{"mcpServers": {"repomap": {"command": "repomap", "args": ["serve", "--repo", "/path/to/repo"]}}}
```

## Honest limits

- **Inferred rationale is a hypothesis.** Every prose claim carries commit SHAs
  and a confidence level so you can check it. Prefer the linked PR discussion
  over the model's reconstruction.
- **Squash-merge repos with "fix" commit messages yield thin history.** The tool
  marks those low-confidence rather than smoothing them into confident narrative.
- **Import resolution is best-effort** and deliberately conservative — a missing
  edge is better than a wrong one. Python gets real AST parsing; JS/TS and others
  get regex extraction. Swapping in tree-sitter or an LSP client is the obvious
  next upgrade, and only `static.py` would change.
- **Flows follow the first dependency edge**, so they're a starting sketch, not a
  traced execution. A real tracer or profiler run would be strictly better.

## Where to extend

`static.py` is the seam for better parsing (tree-sitter, LSP `callHierarchy`).
`gitmine.py` is the seam for richer history (GitHub/GitLab MCP for PR bodies and
review comments, which is where the actual arguments live).
