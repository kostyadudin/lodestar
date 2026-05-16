# Lodestar

Lodestar is a local-first repository context tool for LLM agents. It builds a per-repo index inside `/.lodestar/`, returns bounded context packs, and keeps agent workflows grounded in summaries and evidence instead of large raw file dumps.

## Install

```bash
pip install lodestar-mcp                # primary install
pip install "lodestar-mcp[parsers]"     # add tree-sitter parsers
pip install "lodestar-mcp[embeddings]"  # add sentence-transformers dense retrieval
```

The package ships on PyPI as `lodestar-mcp`; the import path remains `lodestar`.
The `lodestar` and `lodestar-mcp` console scripts are available after install.

## Current scope

- repository scanning and role detection
- per-repo SQLite index at `/.lodestar/index.db`
- incremental refresh by file hash
- repo overview generation
- subsystem summaries
- symbol extraction — heuristic baseline + optional tree-sitter for Python, JS, TS, Go, Rust, Java, Ruby, PHP (`pip install "lodestar-mcp[parsers]"`)
- relation graph between files and symbols
- search, retrieve, explain, remember, **pack**, **timeline**, **capture**, **locate-symbol** primitives
- hybrid ranking — FTS5 BM25 (porter-stemmed) + exact token matching + cosine-style overlap + role-based boost (`source` ×1.3, `documentation` ×0.7) + optional dense semantic similarity
- **symbol-aware ranking v2** — kind boosts (class/function > section), graph-proximity (results connected via relations get a bonus), and file-recency multipliers. Gate with `"ranking_v2": false` in `/.lodestar/config.json` to fall back to v1.
- optional semantic retrieval via sentence-transformers embeddings (`pip install "lodestar-mcp[embeddings]"`) — covers files, symbols, subsystems **and memories**
- memory store with evidence-hash staleness detection, chunk-level evidence refs, `last_validated_at` freshness tracking, aggressive stale suppression, and dense memory recall
- query result caching (retrieve and search)
- repo-local configuration via `/.lodestar/config.json`
- `elapsed_ms` on index, refresh, search, retrieve, pack, timeline, and explain responses
- `eval` command — fixture-based precision@K benchmarking with `--fixture`, `--top-k`, per-query `found_refs`/`missing_refs`, and `avg_precision`
- MCP stdio server with parse-error recovery, `result.isError` tool errors, `-32602` invalid-param responses, and stderr protocol logging
- CLI commands

## Storage layout

Every indexed repository gets a local state directory:

```text
/.lodestar/
  index.db
  config.json   (optional — repo-local policy overrides)
  cache/
  logs/
  state/
  version.json
```

## Repo-local configuration

Create `/.lodestar/config.json` in any indexed repository to override indexing and retrieval behaviour without changing Lodestar itself.

```json
{
  "extra_excludes": ["bootstrap/cache", "public/build"],
  "include_overrides": ["bootstrap/app.php"],
  "role_overrides": {
    "app/Models/*.php": "source",
    "config/*.php": "config"
  },
  "parser_overrides": {
    "php": false
  },
  "retrieval_defaults": {
    "budget_tokens": 2400,
    "limit": 12
  }
}
```

All keys are optional. Missing or malformed config silently falls back to global defaults.

| Key | Type | Effect |
|---|---|---|
| `extra_excludes` | `string[]` | Additional directory names to skip during scanning (same semantics as built-in `EXCLUDED_DIRS`) |
| `include_overrides` | `string[]` | Glob patterns (relative path) that bypass all exclusion rules |
| `role_overrides` | `{glob: role}` | Override the inferred role for paths matching a glob, applied before built-in heuristics |
| `parser_overrides` | `{language: bool}` | Set `false` to disable symbol extraction for a language (file is still indexed, just without symbols) |
| `retrieval_defaults` | `{budget_tokens?, limit?}` | Default token budget and result limit when the caller does not specify them explicitly |

## CLI

```bash
lodestar index /path/to/repo
lodestar refresh /path/to/repo
lodestar overview /path/to/repo
lodestar search /path/to/repo "auth middleware"
lodestar retrieve /path/to/repo "where is auth enforced?" --budget 1800
lodestar pack /path/to/repo "where is auth enforced?" --budget 1800
lodestar explain /path/to/repo "config loading"
lodestar remember /path/to/repo "auth path" "Authentication starts in middleware." --evidence src/auth.py
lodestar timeline /path/to/repo --since last_index
lodestar capture /path/to/repo --from json --source memories.json --commit
lodestar capture /path/to/repo --from claude-jsonl --source ~/.claude/projects/<slug>/<session>.jsonl
lodestar locate-symbol /path/to/repo LodestarService.search
lodestar eval /path/to/repo
lodestar eval /path/to/repo --queries "auth middleware" "config loading" "database schema"
lodestar eval /path/to/repo --fixture /path/to/fixtures.json --top-k 10
```

## MCP

After `pip install lodestar-mcp`, run the stdio server with:

```bash
lodestar-mcp
```

Example Claude Desktop config:

```json
{
  "mcpServers": {
    "lodestar": {
      "command": "lodestar-mcp"
    }
  }
}
```

### Development (running from source)

If you are running from this repository without installing the package, start the MCP server like this:

```bash
PYTHONPATH=src python3 -m lodestar.mcp_server
```

You can also use the included wrapper script:

```bash
./scripts/lodestar-mcp-stdio
```

Claude Desktop config file on macOS:

```text
~/Library/Application Support/Claude/claude_desktop_config.json
```

After restarting Claude Desktop, call Lodestar tools with the target repository path in `repo_root`, for example:

```json
{
  "repo_root": "/path/to/your/project"
}
```

Supported tool names:

- `project.index`
- `project.refresh`
- `project.overview`
- `project.search`
- `project.retrieve`
- `project.pack`
- `project.explain`
- `project.remember`
- `project.timeline`
- `project.capture`
- `project.locate_symbol_range`
- `project.find_usages`

## Notes

- The baseline is intentionally standard-library-first so it can run without extra dependencies.
- Search uses hybrid ranking: FTS5 BM25 (porter-stemmed) + exact token matching + cosine-style term overlap + role-based multipliers + optional dense semantic similarity + graph-neighbor expansion in `retrieve`.
- Install `pip install lodestar[parsers]` to enable tree-sitter symbol extraction for Python, JS, TS, Go, Rust, Java, Ruby, and PHP. Without it, Lodestar falls back to heuristic regex parsing. Tree-sitter gives accurate `ClassName.method` naming and proper nested scope handling.
- Install `pip install lodestar[embeddings]` to enable dense semantic retrieval. This embeds file, symbol, and subsystem summaries using `all-MiniLM-L6-v2` (via `sentence-transformers`) and blends cosine similarity scores into every search and retrieve call. Useful for natural-language queries that don't share vocabulary with identifiers in the code.
- Evidence refs passed to `remember` support `file:`, `symbol:`, and `chunk:` prefixes for increasingly precise staleness detection. Stale memories are suppressed in retrieval unless no fresh memories match the query.
- The MCP server logs protocol-level failures (framing errors, unknown methods) to stderr. Tool execution errors are returned as `result.isError: true` per the MCP spec, not as JSON-RPC error objects.
