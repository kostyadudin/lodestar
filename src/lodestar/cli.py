"""CLI entrypoint for Lodestar."""

from __future__ import annotations

import argparse
import json
import sys

from .indexer import LodestarService


# Built-in fixtures for the Lodestar repo itself.
# Each entry: {query, expected_refs} where expected_refs are substrings
# matched against returned ref strings (e.g. path fragments).
_DEFAULT_EVAL_FIXTURES: list[dict] = [
    {
        "query": "indexing and storage",
        "expected_refs": ["src/lodestar/indexer.py", "src/lodestar/storage.py"],
    },
    {
        "query": "search ranking scoring",
        "expected_refs": ["src/lodestar/indexer.py"],
    },
    {
        "query": "configuration settings",
        "expected_refs": ["src/lodestar/config.py", "src/lodestar/repo_config.py"],
    },
    {
        "query": "symbol extraction parsing",
        "expected_refs": ["src/lodestar/analyzer.py"],
    },
    {
        "query": "memory retrieval",
        "expected_refs": ["src/lodestar/indexer.py"],
    },
]


def _run_eval(
    service: LodestarService,
    repo_root: str,
    fixtures: list[dict],
    top_k: int = 5,
) -> dict:
    """Run precision/recall evaluation against expected evidence refs.

    Each fixture is ``{query: str, expected_refs?: list[str]}``.
    A pass requires every expected_ref to appear as a substring of at least
    one ref in the top-``top_k`` results.  Fixtures without expected_refs
    pass when any result is returned (hit-count check only).
    """
    import time

    results = []
    for fixture in fixtures:
        query = fixture["query"]
        expected = fixture.get("expected_refs", [])
        t0 = time.perf_counter()
        hits = service.search(repo_root, query)["results"]
        elapsed = round((time.perf_counter() - t0) * 1000)

        hit_refs = [h["ref"] for h in hits[:top_k]]
        if expected:
            found = [e for e in expected if any(e in r for r in hit_refs)]
            missing = [e for e in expected if e not in found]
            precision = round(len(found) / len(expected), 3)
            passed = not missing
        else:
            found, missing = [], []
            precision = 1.0 if hits else 0.0
            passed = bool(hits)

        results.append(
            {
                "query": query,
                "hits": len(hits),
                "top_ref": hits[0]["ref"] if hits else None,
                "top_score": round(hits[0]["score"], 4) if hits else 0.0,
                "elapsed_ms": elapsed,
                "precision": precision,
                "found_refs": found,
                "missing_refs": missing,
                "pass": passed,
            }
        )

    passed_count = sum(1 for r in results if r["pass"])
    avg_precision = round(sum(r["precision"] for r in results) / len(results), 3) if results else 0.0
    return {
        "queries_run": len(results),
        "passed": passed_count,
        "failed": len(results) - passed_count,
        "recall": round(passed_count / len(results), 3) if results else 0.0,
        "avg_precision": avg_precision,
        "total_elapsed_ms": sum(r["elapsed_ms"] for r in results),
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lodestar", description="Local-first repository context tool")
    sub = parser.add_subparsers(dest="command", required=True)

    index_cmd = sub.add_parser("index", help="Index a repository")
    index_cmd.add_argument("repo_root")

    refresh_cmd = sub.add_parser("refresh", help="Refresh a repository index")
    refresh_cmd.add_argument("repo_root")
    refresh_cmd.add_argument("changed_paths", nargs="*")

    overview_cmd = sub.add_parser("overview", help="Summarize a repository")
    overview_cmd.add_argument("repo_root")

    search_cmd = sub.add_parser("search", help="Search indexed files")
    search_cmd.add_argument("repo_root")
    search_cmd.add_argument("query")
    search_cmd.add_argument("--kind")
    search_cmd.add_argument("--limit", type=int, default=None)

    retrieve_cmd = sub.add_parser("retrieve", help="Build a bounded context pack")
    retrieve_cmd.add_argument("repo_root")
    retrieve_cmd.add_argument("query")
    retrieve_cmd.add_argument("--budget", type=int, default=None)
    retrieve_cmd.add_argument("--scope")

    explain_cmd = sub.add_parser("explain", help="Explain a subject using indexed context")
    explain_cmd.add_argument("repo_root")
    explain_cmd.add_argument("subject")
    explain_cmd.add_argument("--depth")

    remember_cmd = sub.add_parser("remember", help="Store a durable memory")
    remember_cmd.add_argument("repo_root")
    remember_cmd.add_argument("title")
    remember_cmd.add_argument("summary")
    remember_cmd.add_argument("--evidence", action="append", default=[])

    eval_cmd = sub.add_parser("eval", help="Run a recall evaluation against the index")
    eval_cmd.add_argument("repo_root")
    eval_cmd.add_argument("--queries", nargs="+", default=None, help="Ad-hoc queries (no expected refs; overridden by --fixture)")
    eval_cmd.add_argument("--fixture", default=None, help="Path to JSON fixture file [{query, expected_refs?}]")
    eval_cmd.add_argument("--top-k", type=int, default=5, dest="top_k", help="Top-K hits to check for expected refs (default: 5)")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    service = LodestarService()

    match args.command:
        case "index":
            payload = service.index(args.repo_root)
        case "refresh":
            payload = service.refresh(args.repo_root, args.changed_paths or None)
        case "overview":
            payload = service.overview(args.repo_root)
        case "search":
            payload = service.search(args.repo_root, args.query, kind=args.kind, limit=args.limit)
        case "retrieve":
            payload = service.retrieve(args.repo_root, args.query, budget_tokens=args.budget, scope=args.scope)
        case "explain":
            payload = service.explain(args.repo_root, args.subject, depth=args.depth)
        case "remember":
            payload = service.remember(args.repo_root, args.title, args.summary, evidence_refs=args.evidence)
        case "eval":
            if args.fixture:
                with open(args.fixture) as fh:
                    fixtures = json.load(fh)
            elif args.queries:
                fixtures = [{"query": q} for q in args.queries]
            else:
                fixtures = _DEFAULT_EVAL_FIXTURES
            payload = _run_eval(service, args.repo_root, fixtures, top_k=args.top_k)
        case _:
            parser.print_help(sys.stderr)
            return 1

    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
