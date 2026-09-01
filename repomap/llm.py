"""Layer 2: the only place tokens are spent.

Two rules keep the bill small:
  1. The model never sees source code -- only signatures, paths, commit
     subjects. Enough to name a thing; not enough to re-derive it.
  2. Every result is cached under a hash of its exact input. Re-running the
     build on an unchanged repo costs zero.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from .config import Config, sha1

API_URL = "https://api.anthropic.com/v1/messages"


class Cache:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def _p(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str):
        p = self._p(key)
        if p.exists():
            self.hits += 1
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                return None
        self.misses += 1
        return None

    def put(self, key: str, value) -> None:
        self._p(key).write_text(json.dumps(value, ensure_ascii=False))


class LLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cache = Cache(cfg.cache_dir)
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.calls = 0
        self.in_tokens = 0
        self.out_tokens = 0

    @property
    def available(self) -> bool:
        return bool(self.api_key) and self.cfg.enable_llm

    def _post(self, system: str, user: str, max_tokens: int = 1200) -> str:
        body = json.dumps({
            "model": self.cfg.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request(API_URL, data=body, headers={
            "content-type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        })
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Anthropic API {e.code}: {e.read()[:300]!r}") from None
        self.calls += 1
        usage = data.get("usage", {})
        self.in_tokens += usage.get("input_tokens", 0)
        self.out_tokens += usage.get("output_tokens", 0)
        return "".join(b.get("text", "") for b in data.get("content", []))

    def _json_call(self, key: str, system: str, user: str, max_tokens: int = 1500):
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        if not self.available:
            return None
        text = self._post(system, user, max_tokens).strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = {"_raw": text}
        self.cache.put(key, value)
        return value

    # --- module summaries ---------------------------------------------------

    MODULE_SYSTEM = (
        "You summarise software modules from their public surface alone. "
        "You never see the implementation, so never claim to know it. "
        "Return JSON only, no prose, no markdown fences. Schema: "
        '{"modules":[{"name":str,"purpose":str,"role":str}]} '
        "purpose: one sentence, max 25 words, concrete, no filler like "
        "'this module handles'. role: one of "
        "api|domain|persistence|infrastructure|ui|shared|config|test|unknown."
    )

    def module_summaries(self, batch: list[dict]) -> dict[str, dict]:
        """Batched: many small modules in one call beats one call per module."""
        out: dict[str, dict] = {}
        pending = []
        for m in batch:
            key = "mod-" + sha1(json.dumps(m, sort_keys=True))[:20]
            cached = self.cache.get(key)
            if cached is not None:
                out[m["name"]] = cached
            else:
                pending.append((key, m))
        if not pending or not self.available:
            for _, m in pending:
                out[m["name"]] = {"purpose": "", "role": "unknown"}
            return out

        for i in range(0, len(pending), 12):
            group = pending[i: i + 12]
            payload = json.dumps([m for _, m in group], ensure_ascii=False)
            text = self._post(self.MODULE_SYSTEM, payload, max_tokens=1600).strip()
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            try:
                parsed = json.loads(text).get("modules", [])
            except json.JSONDecodeError:
                parsed = []
            by_name = {p.get("name"): p for p in parsed if isinstance(p, dict)}
            for key, m in group:
                val = by_name.get(m["name"], {"purpose": "", "role": "unknown"})
                val = {"purpose": val.get("purpose", ""), "role": val.get("role", "unknown")}
                self.cache.put(key, val)
                out[m["name"]] = val
        return out

    # --- episode summaries --------------------------------------------------

    EPISODE_SYSTEM = (
        "You write the history section of an engineering handbook. Given commit "
        "subjects and metadata for one period of work on one feature, write what "
        "changed and, only if the evidence supports it, why. "
        "Rules: 2-4 sentences. Past tense. Name concrete things. "
        "If the evidence does not state a reason, describe the change and stop -- "
        "do NOT invent motivation. Never write 'likely', 'presumably', or "
        "'appears to' as a way to smuggle in a guess; omit instead. "
        "Return JSON only: "
        '{"summary":str,"confidence":"high"|"medium"|"low","decision":str|null} '
        "decision: a one-line durable decision a future reader must know, or null."
    )

    def episode_summary(self, ep_payload: dict) -> dict:
        key = "ep-" + sha1(json.dumps(ep_payload, sort_keys=True))[:20]
        res = self._json_call(key, self.EPISODE_SYSTEM,
                              json.dumps(ep_payload, ensure_ascii=False), 700)
        if not isinstance(res, dict) or "summary" not in res:
            return {"summary": "", "confidence": "low", "decision": None}
        return res

    # --- flow naming --------------------------------------------------------

    FLOW_SYSTEM = (
        "You name and describe execution flows in a codebase. Input is an entry "
        "point plus the ordered chain of modules it reaches. Return JSON only: "
        '{"name":str,"description":str} '
        "name: 2-5 words, human, e.g. 'Checkout payment authorisation'. "
        "description: max 40 words, what happens along this path. "
        "Base it strictly on the given names; do not invent steps."
    )

    def flow_summary(self, payload: dict) -> dict:
        key = "flow-" + sha1(json.dumps(payload, sort_keys=True))[:20]
        res = self._json_call(key, self.FLOW_SYSTEM,
                              json.dumps(payload, ensure_ascii=False), 400)
        if not isinstance(res, dict) or "name" not in res:
            return {"name": payload.get("entry", "flow"), "description": ""}
        return res

    def stats(self) -> dict:
        return {
            "api_calls": self.calls,
            "input_tokens": self.in_tokens,
            "output_tokens": self.out_tokens,
            "cache_hits": self.cache.hits,
            "cache_misses": self.cache.misses,
        }
