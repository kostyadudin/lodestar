# Changelog

All notable changes to this project are documented here.
This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.1] — 2026-05-16

Distribution renamed from `lodestar-mcp` to `lodestar-context` to clear PyPI's
name-similarity check against the unrelated, abandoned 2018 `Lodestar` package.
Import path (`lodestar`) and CLI binaries (`lodestar`, `lodestar-mcp`) are
unchanged; only `pip install` users are affected.

### Changed

- `pyproject.toml`: `name = "lodestar-context"`, version bump to 0.2.1.
- README and release workflow updated to reference the new distribution name.

## [0.2.0] — 2026-05-16 (unreleased — pre-rename)

Distribution name changed to `lodestar-mcp` for PyPI. The import path remains
`lodestar`; existing CLI commands (`lodestar`, `lodestar-mcp`) are unchanged.

### Added

- **Symbol-aware retrieval ranking** — search and retrieve now apply three
  boosts on top of BM25 + role multipliers: symbol-kind (class/function over
  generic sections), graph-proximity (refs connected via the relations graph),
  and file recency. Gated by `ranking_v2` in `.lodestar/config.json` (default
  on).
- **`project.pack`** MCP tool and `lodestar pack` CLI — emits a deterministic
  JSON envelope of token-budgeted sections (overview, subsystem summaries,
  symbols, chunks, memories, edges) with scores, evidence refs, and a
  `dropped[]` list for transparency.
- **Memory embeddings** — when the `[embeddings]` extra is installed, memory
  bodies are encoded and cosine similarity is blended into recall scores.
  Falls back silently to FTS-only when the embedder is unavailable.
- **`project.timeline`** MCP tool and `lodestar timeline` CLI — chronological
  deltas across `file_changed`, `memory_added`, and `memory_stale` events.
  Accepts `since` as an ISO timestamp or `last_index` / `last_refresh`, and a
  `scope` filter.
- **`project.capture`** MCP tool and `lodestar capture` CLI — ingest
  pre-structured JSON (`--from json`) or Claude Code session logs (`--from
  claude-jsonl`) into the memory store. Heuristic-only, no LLM in the
  pipeline; defaults to `--dry-run`, requires `--commit` to write.
- **`project.locate_symbol_range`** MCP tool and `lodestar locate-symbol` CLI —
  resolve a symbol by name (or dotted path) to its byte/line range. Returns
  all matches with disambiguation scores. Read-only.

### Changed

- Search cache key now incorporates the ranking version so v1↔v2 flips don't
  serve stale results.
- `_rebuild_embeddings` now includes memory rows in its candidate set so a
  single re-index brings memory vectors up to date.

## [0.1.0] — initial

- Repository scanning, role detection, per-repo SQLite/FTS5 index.
- MCP stdio server with `project.index`, `project.refresh`, `project.overview`,
  `project.search`, `project.retrieve`, `project.explain`, `project.remember`,
  `project.find_usages`.
- Optional tree-sitter parsers and sentence-transformers embeddings.
- Memory store with evidence-hash staleness, chunk-level evidence refs,
  freshness tracking.
- Fixture-based `lodestar eval` recall benchmark.
