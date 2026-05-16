"""Token-budgeted context bundle: project.pack.

Composes a deterministic JSON envelope with sections from overview, subsystems,
symbol/chunk hits, relevant memories, and graph edges. Greedy packing under a
token budget; dropped sections are surfaced in stats for transparency.
"""

from __future__ import annotations

import time
from typing import Any, TYPE_CHECKING

from .config import DB_FILENAME
from .repo_config import RepoConfig
from .storage import connect
from .utils import token_estimate

if TYPE_CHECKING:
    from .indexer import LodestarService


SECTION_KINDS = ("overview", "subsystem_summary", "symbol", "chunk", "memory", "edge")
MAX_SEARCH_RESULTS = 10
MAX_SUBSYSTEMS = 3
MAX_EDGES = 3


def build_pack(
    service: "LodestarService",
    repo_root: str,
    query: str,
    budget_tokens: int | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    root = service._repo_root(repo_root)
    state = service._ensure_state(root)
    cfg = RepoConfig.from_state(state)
    budget = cfg.effective_budget(budget_tokens)
    conn = connect(state / DB_FILENAME)

    overview = service.overview(repo_root)
    search_response = service.search(repo_root, query, kind=scope, limit=MAX_SEARCH_RESULTS)
    search_results = search_response["results"]
    memories = service._relevant_memories(conn, query)

    candidates: list[dict[str, Any]] = []

    # Overview is grounding context; surface it before scored hits.
    # The score is high enough to win sort order without skewing comparisons between hits.
    overview_body = overview["summary"]
    candidates.append(_section("overview", 1000.0, [], overview_body))

    for i, summary in enumerate(overview["subsystems"][:MAX_SUBSYSTEMS]):
        candidates.append(_section("subsystem_summary", round(900.0 - i, 3), [], summary))

    for result in search_results:
        candidates.extend(_candidates_for_result(conn, result))

    # Curated memories are high-signal — rank above typical chunk scores so they
    # survive tight budgets. Stale memories drop to advisory tier.
    for memory in memories:
        body = f"{memory.title}: {memory.summary}"
        score = 7.0 if not memory.stale else 0.5
        evidence = list(memory.evidence_refs) or [f"memory:{memory.memory_id}"]
        candidates.append(_section("memory", score, evidence, body))

    candidates.extend(_edge_candidates(conn, candidates))

    candidates.sort(key=lambda c: (-c["score"], c["kind"], c["body"][:40]))

    sections: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    used = 0
    seen_bodies: set[str] = set()
    for cand in candidates:
        body_key = f'{cand["kind"]}|{cand["body"]}'
        if body_key in seen_bodies:
            continue
        seen_bodies.add(body_key)
        cost = int(cand["tokens"])
        if used + cost > budget:
            dropped.append(
                {
                    "kind": cand["kind"],
                    "score": cand["score"],
                    "tokens": cost,
                    "reason": "budget_exhausted",
                }
            )
            continue
        sections.append(cand)
        used += cost

    return {
        "query": query,
        "repo_root": str(root),
        "budget_tokens": budget,
        "used_tokens": used,
        "sections": sections,
        "stats": {
            "dropped": dropped,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
            "ranking_v2": cfg.ranking_v2,
            "section_counts": _section_counts(sections),
        },
    }


def _section(kind: str, score: float, evidence_refs: list[str], body: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "score": round(float(score), 3),
        "evidence_refs": list(evidence_refs),
        "tokens": token_estimate(body),
        "body": body,
    }


def _candidates_for_result(conn, result: dict[str, Any]) -> list[dict[str, Any]]:
    ref = result["ref"]
    score = float(result["score"])
    out: list[dict[str, Any]] = []
    if ref.startswith("symbol:"):
        row = conn.execute(
            "SELECT symbol_id, signature, summary FROM symbols WHERE symbol_id = ?",
            (ref,),
        ).fetchone()
        if row is not None:
            body = f'{row["signature"]}\n{row["summary"]}' if row["signature"] else row["summary"]
            out.append(_section("symbol", score, [ref], body))
        for chunk in conn.execute(
            "SELECT chunk_id, text, token_estimate FROM chunks WHERE symbol_id = ? ORDER BY line_start",
            (ref,),
        ).fetchall():
            out.append(
                {
                    "kind": "chunk",
                    "score": round(score * 0.9, 3),
                    "evidence_refs": [f'chunk:{chunk["chunk_id"]}'],
                    "tokens": int(chunk["token_estimate"]),
                    "body": chunk["text"],
                }
            )
    elif ref.startswith("file:"):
        path = ref.split(":", 1)[1]
        for chunk in conn.execute(
            "SELECT chunk_id, text, token_estimate FROM chunks WHERE path = ? "
            "ORDER BY token_estimate ASC, line_start ASC LIMIT 2",
            (path,),
        ).fetchall():
            out.append(
                {
                    "kind": "chunk",
                    "score": score,
                    "evidence_refs": [f'chunk:{chunk["chunk_id"]}'],
                    "tokens": int(chunk["token_estimate"]),
                    "body": chunk["text"],
                }
            )
    return out


def _edge_candidates(conn, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    graph_refs: set[str] = set()
    for c in candidates:
        for ev in c["evidence_refs"]:
            if ev.startswith("symbol:"):
                graph_refs.add(ev)
            elif ev.startswith("chunk:"):
                chunk_id = ev.removeprefix("chunk:")
                row = conn.execute(
                    "SELECT path, symbol_id FROM chunks WHERE chunk_id = ?", (chunk_id,)
                ).fetchone()
                if row is not None:
                    if row["symbol_id"]:
                        graph_refs.add(row["symbol_id"])
                    graph_refs.add(f'file:{row["path"]}')
    if not graph_refs:
        return []
    placeholders = ",".join("?" * len(graph_refs))
    refs_tuple = tuple(graph_refs)
    edges = conn.execute(
        f"SELECT source_ref, target_ref, relation_type, weight FROM relations "
        f"WHERE source_ref IN ({placeholders}) AND target_ref IN ({placeholders}) "
        f"AND source_ref != target_ref "
        f"ORDER BY weight DESC LIMIT ?",
        refs_tuple + refs_tuple + (MAX_EDGES,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for edge in edges:
        body = f'{edge["source_ref"]} -[{edge["relation_type"]}]-> {edge["target_ref"]}'
        out.append(
            _section(
                "edge",
                round(0.3 * float(edge["weight"]), 3),
                [edge["source_ref"], edge["target_ref"]],
                body,
            )
        )
    return out


def _section_counts(sections: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in sections:
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
    return counts
