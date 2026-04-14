"""Repository indexing and retrieval logic."""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from .analyzer import (
    build_chunks,
    build_relations,
    cosineish_score,
    derive_subsystems,
    extract_symbols,
    fts_query,
    query_vector,
    subsystem_name_for_path,
)
from .config import (
    DB_FILENAME,
    EXCLUDED_DIRS,
    EXCLUDED_SUFFIXES,
    LANGUAGE_BY_EXTENSION,
    MAX_FILE_BYTES,
    QUERY_CACHE_LIMIT,
    ENTRYPOINT_NAMES,
    ROLE_HINTS,
    ROOT_FILES,
    TEXT_EXTENSIONS,
    VERSION_FILENAME,
    state_path,
)
from .models import CodeChunk, ContextPack, EvidenceRef, MemoryEntry, SearchResult, SymbolSummary
from .repo_config import RepoConfig
from . import embedder
from .storage import connect, rebuild_fts
from .utils import ensure_json, sha256_bytes, token_estimate


SCHEMA_VERSION = "4"
SEMANTIC_WEIGHT = 0.3
# Applied as a final multiplier on each result's total score.
# Boosts code files over documentation to counteract BM25's verbosity bias.
ROLE_SCORE_BOOST: dict[str, float] = {
    "entrypoint": 1.4,
    "source": 1.3,
    "route": 1.3,
    "model": 1.3,
    "controller": 1.2,
    "middleware": 1.15,
    "view": 1.1,
    "test": 1.1,
    "documentation": 0.7,
    "agent-guidance": 0.6,
}


class LodestarService:
    """Core service used by the CLI and MCP surface."""

    def index(self, repo_root: str, options: dict | None = None) -> dict:
        del options
        t0 = time.perf_counter()
        root = self._repo_root(repo_root)
        state = self._ensure_state(root)
        cfg = RepoConfig.from_state(state)
        conn = connect(state / DB_FILENAME)

        # Force full re-extraction when the schema version has changed (e.g. after a parser upgrade)
        stored_version = self._get_meta(conn, "schema_version")
        force_reindex = stored_version != SCHEMA_VERSION
        if force_reindex:
            for table in ("symbols", "chunks", "relations"):
                conn.execute(f"DELETE FROM {table}")

        indexed = 0
        skipped = 0
        indexed_paths: set[str] = set()
        file_rows: list[dict] = []
        symbol_rows: list[dict] = []
        relation_rows: list[dict] = []

        for path in self._iter_files(root, cfg):
            rel_path = path.relative_to(root).as_posix()
            file_record = self._build_file_record(root, path, cfg)
            if file_record is None:
                conn.execute("DELETE FROM files WHERE path = ?", (rel_path,))
                skipped += 1
                continue
            indexed_paths.add(rel_path)
            indexed += 1
            file_rows.append(file_record)
            symbol_rows.extend(file_record["symbols"])
            relation_rows.extend(file_record["relations"])
            self._upsert_file(conn, file_record, force=force_reindex)

        self._delete_missing(conn, indexed_paths)
        self._rebuild_subsystems(conn, file_rows, symbol_rows)
        self._rebuild_relations(conn, relation_rows)
        rebuild_fts(conn)
        self._rebuild_embeddings(conn, full=True)
        self._set_meta(conn, "repo_root", str(root))
        self._set_meta(conn, "last_indexed_at", self._now())
        self._set_meta(conn, "schema_version", SCHEMA_VERSION)
        self._clear_query_cache(conn)
        conn.commit()
        ensure_json(
            state / VERSION_FILENAME,
            {
                "app": "lodestar",
                "version": int(SCHEMA_VERSION),
                "repo_root": str(root),
                "last_indexed_at": self._now(),
            },
        )
        return {
            "repo_root": str(root),
            "indexed_files": indexed,
            "skipped_files": skipped,
            "state_dir": str(state),
            "db_path": str(state / DB_FILENAME),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
        }

    def refresh(self, repo_root: str, changed_paths: list[str] | None = None) -> dict:
        t0 = time.perf_counter()
        root = self._repo_root(repo_root)
        state = self._ensure_state(root)
        cfg = RepoConfig.from_state(state)
        conn = connect(state / DB_FILENAME)
        updated = 0
        deleted = 0

        paths = [root / item for item in changed_paths] if changed_paths else list(self._iter_files(root, cfg))
        seen: set[str] = set()
        for path in paths:
            rel_path = path.relative_to(root).as_posix()
            seen.add(rel_path)
            if not path.exists():
                conn.execute("DELETE FROM files WHERE path = ?", (rel_path,))
                deleted += 1
                continue
            file_record = self._build_file_record(root, path, cfg)
            if file_record is None:
                conn.execute("DELETE FROM files WHERE path = ?", (rel_path,))
                continue
            self._upsert_file(conn, file_record)
            updated += 1

        if not changed_paths:
            all_indexed = {row["path"] for row in conn.execute("SELECT path FROM files")}
            for rel_path in all_indexed - seen:
                conn.execute("DELETE FROM files WHERE path = ?", (rel_path,))
                deleted += 1

        self._rebuild_derived_tables(conn)
        rebuild_fts(conn)
        self._rebuild_embeddings(conn, full=False)
        self._set_meta(conn, "last_refreshed_at", self._now())
        self._clear_query_cache(conn)
        conn.commit()
        return {
            "repo_root": str(root),
            "updated_files": updated,
            "deleted_files": deleted,
            "state_dir": str(state),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
        }

    def overview(self, repo_root: str) -> dict:
        root = self._repo_root(repo_root)
        conn = connect(self._ensure_state(root) / DB_FILENAME)

        total_files = conn.execute("SELECT COUNT(*) AS count FROM files").fetchone()["count"]
        languages = Counter(
            {
                row["language"]: row["count"]
                for row in conn.execute(
                    "SELECT language, COUNT(*) AS count FROM files GROUP BY language ORDER BY count DESC"
                )
            }
        )
        roles = Counter(
            {
                row["role"]: row["count"]
                for row in conn.execute("SELECT role, COUNT(*) AS count FROM files GROUP BY role ORDER BY count DESC")
            }
        )
        key_files = [
            row["path"]
            for row in conn.execute(
                "SELECT path FROM files WHERE path IN ({}) ORDER BY path".format(",".join("?" for _ in ROOT_FILES)),
                tuple(ROOT_FILES),
            )
        ]
        top_dirs = Counter()
        for row in conn.execute("SELECT path FROM files"):
            path = row["path"]
            top_dirs[path.split("/", 1)[0] if "/" in path else "."] += 1

        summary = self._overview_summary(root.name, total_files, languages, roles, key_files, top_dirs)
        subsystem_summaries = [row["summary"] for row in conn.execute("SELECT summary FROM subsystems ORDER BY file_count DESC, name ASC")]
        return {
            "repo_root": str(root),
            "summary": summary,
            "languages": dict(languages),
            "roles": dict(roles),
            "key_files": key_files,
            "subsystems": subsystem_summaries[:8],
        }

    def search(self, repo_root: str, query: str, kind: str | None = None, limit: int | None = None) -> dict:
        t0 = time.perf_counter()
        root = self._repo_root(repo_root)
        state = self._ensure_state(root)
        cfg = RepoConfig.from_state(state)
        effective_limit = cfg.effective_limit(limit)
        conn = connect(state / DB_FILENAME)
        query_terms = query_vector(query)
        if not query_terms:
            return {"results": [], "elapsed_ms": 0}

        cache_key = self._search_cache_key(query, kind, effective_limit)
        cached = self._get_cached_query(conn, cache_key)
        if cached is not None:
            return cached

        bm25 = self._fts_scores(conn, query)
        semantic = self._semantic_scores(conn, query)

        rows: list[SearchResult] = []
        for row in conn.execute("SELECT path, role, language, summary FROM files"):
            ref = f'file:{row["path"]}'
            score = self._file_score(row["path"], row["summary"], row["role"], row["language"], query_terms)
            score += bm25.get(ref, 0.0)
            score += SEMANTIC_WEIGHT * semantic.get(ref, 0.0)
            score *= ROLE_SCORE_BOOST.get(row["role"], 1.0)
            if kind and row["role"] != kind and row["language"] != kind:
                score *= 0.5
            if score > 0:
                rows.append(
                    SearchResult(
                        ref=ref,
                        path=row["path"],
                        name=row["path"].split("/")[-1],
                        kind="file",
                        role=row["role"],
                        language=row["language"],
                        score=round(score, 3),
                        summary=row["summary"],
                    )
                )

        for row in conn.execute(
            "SELECT s.symbol_id, s.path, s.name, s.kind, s.summary,"
            " COALESCE(f.role, 'source') AS file_role"
            " FROM symbols s LEFT JOIN files f ON s.path = f.path"
        ):
            score = self._symbol_score(row["path"], row["name"], row["kind"], row["summary"], query_terms)
            score += bm25.get(row["symbol_id"], 0.0)
            score += SEMANTIC_WEIGHT * semantic.get(row["symbol_id"], 0.0)
            if score > 0:
                score = (score + 0.25) * ROLE_SCORE_BOOST.get(row["file_role"], 1.0)
                rows.append(
                    SearchResult(
                        ref=row["symbol_id"],
                        path=row["path"],
                        name=row["name"],
                        kind=row["kind"],
                        role=row["file_role"],
                        language="symbol",
                        score=round(score, 3),
                        summary=row["summary"],
                    )
                )

        for row in conn.execute("SELECT name, summary FROM subsystems"):
            ref = f'subsystem:{row["name"]}'
            score = cosineish_score(query_terms, f'{row["name"]} {row["summary"]}')
            score += SEMANTIC_WEIGHT * semantic.get(ref, 0.0)
            if score > 0:
                rows.append(
                    SearchResult(
                        ref=f'subsystem:{row["name"]}',
                        path=row["name"],
                        name=row["name"],
                        kind="subsystem",
                        role="subsystem",
                        language="subsystem",
                        score=round(score + 0.2, 3),
                        summary=row["summary"],
                    )
                )

        rows.sort(key=lambda item: (-item.score, item.kind, item.path, item.name))
        deduped: list[SearchResult] = []
        seen: set[str] = set()
        for item in rows:
            if item.ref in seen:
                continue
            seen.add(item.ref)
            deduped.append(item)
            if len(deduped) >= effective_limit:
                break
        result = {"results": [item.to_dict() for item in deduped], "elapsed_ms": round((time.perf_counter() - t0) * 1000)}
        self._store_cached_query(conn, cache_key, query, result)
        conn.commit()
        return result

    def retrieve(
        self,
        repo_root: str,
        query: str,
        budget_tokens: int | None = None,
        scope: str | None = None,
    ) -> dict:
        t0 = time.perf_counter()
        root = self._repo_root(repo_root)
        state = self._ensure_state(root)
        cfg = RepoConfig.from_state(state)
        budget_tokens = cfg.effective_budget(budget_tokens)
        conn = connect(state / DB_FILENAME)
        cache_key = self._cache_key(query, budget_tokens, scope)
        cached = self._get_cached_query(conn, cache_key)
        if cached is not None:
            return cached

        overview = self.overview(repo_root)
        search_results = self.search(repo_root, query, kind=scope, limit=10)["results"]
        related_results = self._expand_related_results(conn, search_results)

        used_tokens = token_estimate(overview["summary"])
        subsystem_summaries: list[str] = []
        for summary in overview["subsystems"]:
            cost = token_estimate(summary)
            if used_tokens + cost > budget_tokens or len(subsystem_summaries) >= 3:
                break
            subsystem_summaries.append(summary)
            used_tokens += cost

        symbol_summaries: list[dict] = []
        chunks: list[CodeChunk] = []
        evidence_refs: list[EvidenceRef] = []

        for result in related_results:
            candidate = self._materialize_result(conn, result)
            if candidate is None:
                continue
            summary_cost = token_estimate(candidate["summary"])
            if used_tokens + summary_cost > budget_tokens:
                break
            if candidate["kind"] == "symbol":
                symbol_summaries.append(candidate["summary_payload"])
            else:
                symbol_summaries.append(candidate["summary"])
            used_tokens += summary_cost

            for chunk in candidate["chunks"]:
                if used_tokens + chunk.token_estimate > budget_tokens:
                    break
                chunks.append(chunk)
                evidence_refs.append(
                    EvidenceRef(
                        path=chunk.path,
                        chunk_id=chunk.chunk_id,
                        line_start=chunk.line_start,
                        line_end=chunk.line_end,
                    )
                )
                used_tokens += chunk.token_estimate
            if used_tokens >= budget_tokens:
                break

        memories: list[MemoryEntry] = []
        for item in self._relevant_memories(conn, query):
            memory_cost = token_estimate(item.summary)
            if used_tokens + memory_cost > budget_tokens:
                break
            memories.append(item)
            used_tokens += memory_cost

        context = ContextPack(
            repo_summary=overview["summary"],
            subsystem_summaries=subsystem_summaries,
            symbol_summaries=symbol_summaries,
            code_chunks=chunks,
            memories=memories,
            evidence_refs=evidence_refs,
            token_estimate=used_tokens,
        ).to_dict()
        context["elapsed_ms"] = round((time.perf_counter() - t0) * 1000)
        self._store_cached_query(conn, cache_key, query, context)
        conn.commit()
        return context

    def explain(self, repo_root: str, subject: str, depth: str | None = None) -> dict:
        t0 = time.perf_counter()
        del depth
        root = self._repo_root(repo_root)
        conn = connect(self._ensure_state(root) / DB_FILENAME)
        overview = self.overview(repo_root)
        search_results = self.search(repo_root, subject, limit=6)["results"]
        subsystem_hits = [
            row["summary"]
            for row in conn.execute(
                "SELECT summary FROM subsystems WHERE lower(name) LIKE ? OR lower(summary) LIKE ? ORDER BY file_count DESC",
                (f"%{subject.lower()}%", f"%{subject.lower()}%"),
            )
        ]
        explanation = {
            "subject": subject,
            "repo_summary": overview["summary"],
            "relevant_paths": [item["path"] for item in search_results if item["kind"] != "subsystem"],
            "explanation": self._compose_explanation(subject, search_results, subsystem_hits or overview["subsystems"]),
            "evidence_refs": [item["ref"] for item in search_results],
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
        }
        return explanation

    def remember(self, repo_root: str, title: str, summary: str, evidence_refs: list[str] | None = None) -> dict:
        root = self._repo_root(repo_root)
        conn = connect(self._ensure_state(root) / DB_FILENAME)
        evidence = evidence_refs or []
        evidence_hash = self._evidence_hash(conn, evidence)
        created_at = self._now()
        cursor = conn.execute(
            "INSERT INTO memories(title, summary, evidence_refs, evidence_hash, created_at, last_validated_at)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (title, summary, json.dumps(evidence), evidence_hash, created_at, created_at),
        )
        self._clear_query_cache(conn)
        conn.commit()
        return {"memory_id": cursor.lastrowid, "title": title, "created_at": created_at, "last_validated_at": created_at}

    def _repo_root(self, repo_root: str) -> Path:
        root = Path(repo_root).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(f"Repository root does not exist: {repo_root}")
        return root

    def _ensure_state(self, root: Path) -> Path:
        state = state_path(root)
        for child in (state, state / "cache", state / "logs", state / "state"):
            child.mkdir(parents=True, exist_ok=True)
        return state

    def _iter_files(self, root: Path, cfg: RepoConfig) -> Iterable[Path]:
        root_str = str(root)
        root_prefix = root_str + os.sep
        for dirpath, dirs, filenames in os.walk(root_str, topdown=True):
            # Prune excluded dirs before descending — avoids walking vendor/, node_modules/, etc.
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
            for filename in filenames:
                if filename.startswith(".") and filename not in ROLE_HINTS:
                    continue
                full = os.path.join(dirpath, filename)
                rel_path = full[len(root_prefix):].replace(os.sep, "/")
                rel_parts = tuple(rel_path.split("/"))
                if cfg.is_force_included(rel_path):
                    yield Path(full)
                    continue
                if cfg.is_excluded(rel_parts):
                    continue
                suffix = os.path.splitext(filename)[1].lower()
                if suffix in EXCLUDED_SUFFIXES:
                    continue
                yield Path(full)

    def _build_file_record(self, root: Path, path: Path, cfg: RepoConfig) -> dict | None:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        if ".min." in path.name:
            return None
        if path.suffix.lower() not in TEXT_EXTENSIONS and path.name not in ROLE_HINTS:
            return None

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return None

        rel_path = path.relative_to(root).as_posix()
        language = LANGUAGE_BY_EXTENSION.get(path.suffix.lower(), "text")
        role = self._infer_role(rel_path, path.name, cfg)
        subsystem = subsystem_name_for_path(rel_path)
        summary = self._summarize_file(rel_path, role, language, subsystem, content)
        symbols = extract_symbols(rel_path, language, content) if cfg.parser_enabled(language) else []
        chunks = self._build_chunks_for_file(rel_path, content, symbols)
        relations = build_relations(rel_path, content, symbols)
        return {
            "path": rel_path,
            "hash": sha256_bytes(content.encode("utf-8")),
            "size_bytes": len(content.encode("utf-8")),
            "language": language,
            "role": role,
            "summary": summary,
            "updated_at": self._now(),
            "chunks": chunks,
            "symbols": symbols,
            "relations": relations,
        }

    def _infer_role(self, rel_path: str, name: str, cfg: RepoConfig) -> str:
        override = cfg.role_for(rel_path)
        if override:
            return override
        if name in ROLE_HINTS:
            return ROLE_HINTS[name]
        if name in ENTRYPOINT_NAMES:
            return "entrypoint"

        parts = rel_path.lower().split("/")
        dirs = parts[:-1]  # directory components only

        # Routes: dedicated routes directory, Django urls.py, Next.js pages/api/*
        if parts[0] == "routes":
            return "route"
        if len(parts) >= 2 and parts[0] == "pages" and parts[1] == "api":
            return "route"
        if name in {"urls.py", "routes.py", "router.py", "web.php", "api.php", "console.php", "channels.php"}:
            return "route"

        # Models
        if "models" in dirs or name == "models.py":
            return "model"

        # Controllers (Django views.py handles HTTP — functionally a controller)
        if "controllers" in dirs or name == "views.py":
            return "controller"

        # View templates (non-Python files in views/ or any templates/ directory)
        if ("views" in dirs or "templates" in dirs) and not name.endswith(".py"):
            return "view"

        # Middleware
        if "middleware" in dirs or "middlewares" in dirs:
            return "middleware"

        if "/test" in rel_path or rel_path.startswith("tests/") or name.endswith("_test.py"):
            return "test"
        if rel_path.startswith("docs/") or name.endswith(".md"):
            return "documentation"
        if "config" in rel_path or name in {"settings.py", ".env.example"}:
            return "config"
        if rel_path.startswith("scripts/"):
            return "script"
        return "source"

    def _summarize_file(self, rel_path: str, role: str, language: str, subsystem: str, content: str) -> str:
        lines = [line.strip() for line in content.splitlines() if line.strip()][:6]
        snippet = " ".join(lines)[:180] if lines else "Minimal textual content."
        return f"{role} file in {language} for subsystem {subsystem}: {rel_path}. Key content: {snippet}"

    def _build_chunks_for_file(self, rel_path: str, content: str, symbols: list[dict]) -> list[dict]:
        if symbols:
            chunks = []
            for symbol in symbols:
                chunks.append(
                    {
                        "chunk_id": f'{symbol["symbol_id"]}:{symbol["line_start"]}-{symbol["line_end"]}',
                        "line_start": symbol["line_start"],
                        "line_end": symbol["line_end"],
                        "text": symbol["text"],
                        "summary": symbol["summary"],
                        "token_estimate": symbol["token_estimate"],
                        "hash": sha256_bytes(symbol["text"].encode("utf-8")),
                        "chunk_type": "symbol",
                        "symbol_id": symbol["symbol_id"],
                    }
                )
            return chunks
        return [{**chunk, "symbol_id": None} for chunk in build_chunks(rel_path, content)]

    def _upsert_file(self, conn, file_record: dict, force: bool = False) -> None:
        existing = conn.execute("SELECT hash FROM files WHERE path = ?", (file_record["path"],)).fetchone()
        if not force and existing and existing["hash"] == file_record["hash"]:
            return

        conn.execute(
            "INSERT INTO files(path, hash, size_bytes, language, role, summary, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET hash=excluded.hash, size_bytes=excluded.size_bytes, "
            "language=excluded.language, role=excluded.role, summary=excluded.summary, updated_at=excluded.updated_at",
            (
                file_record["path"],
                file_record["hash"],
                file_record["size_bytes"],
                file_record["language"],
                file_record["role"],
                file_record["summary"],
                file_record["updated_at"],
            ),
        )
        conn.execute("DELETE FROM chunks WHERE path = ?", (file_record["path"],))
        conn.execute("DELETE FROM symbols WHERE path = ?", (file_record["path"],))
        conn.execute("DELETE FROM relations WHERE source_ref = ? OR target_ref = ?", (f'file:{file_record["path"]}', f'file:{file_record["path"]}'))

        seen_symbol_ids: set[str] = set()
        for symbol in file_record["symbols"]:
            if symbol["symbol_id"] in seen_symbol_ids:
                continue
            seen_symbol_ids.add(symbol["symbol_id"])
            conn.execute(
                "INSERT INTO symbols(symbol_id, path, name, kind, signature, line_start, line_end, summary, hash, token_estimate) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    symbol["symbol_id"],
                    symbol["path"],
                    symbol["name"],
                    symbol["kind"],
                    symbol["signature"],
                    symbol["line_start"],
                    symbol["line_end"],
                    symbol["summary"],
                    sha256_bytes(symbol["text"].encode("utf-8")),
                    symbol["token_estimate"],
                ),
            )

        seen_chunk_ids: set[str] = set()
        for chunk in file_record["chunks"]:
            if chunk["chunk_id"] in seen_chunk_ids:
                continue
            seen_chunk_ids.add(chunk["chunk_id"])
            conn.execute(
                "INSERT INTO chunks(chunk_id, path, symbol_id, line_start, line_end, text, summary, token_estimate, hash, chunk_type) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk["chunk_id"],
                    file_record["path"],
                    chunk.get("symbol_id"),
                    chunk["line_start"],
                    chunk["line_end"],
                    chunk["text"],
                    chunk["summary"],
                    chunk["token_estimate"],
                    chunk["hash"],
                    chunk.get("chunk_type", "window"),
                ),
            )

        for relation in file_record["relations"]:
            conn.execute(
                "INSERT INTO relations(source_ref, target_ref, relation_type, weight) VALUES(?, ?, ?, ?)",
                (
                    relation["source_ref"],
                    relation["target_ref"],
                    relation["relation_type"],
                    relation["weight"],
                ),
            )

    def _delete_missing(self, conn, indexed_paths: set[str]) -> None:
        existing_paths = {row["path"] for row in conn.execute("SELECT path FROM files")}
        for rel_path in existing_paths - indexed_paths:
            conn.execute("DELETE FROM files WHERE path = ?", (rel_path,))

    def _rebuild_derived_tables(self, conn) -> None:
        file_rows = [dict(row) for row in conn.execute("SELECT path, language, role, summary FROM files")]
        symbol_rows = [dict(row) for row in conn.execute("SELECT symbol_id, path, name, kind, summary FROM symbols")]
        relation_rows: list[dict] = []
        for row in conn.execute("SELECT path FROM files"):
            path = row["path"]
            file_content_rows = conn.execute(
                "SELECT symbol_id, name, kind FROM symbols WHERE path = ? ORDER BY line_start", (path,)
            ).fetchall()
            text_rows = conn.execute("SELECT text FROM chunks WHERE path = ? ORDER BY line_start LIMIT 1", (path,)).fetchone()
            content = text_rows["text"] if text_rows else ""
            symbols = [
                {"symbol_id": item["symbol_id"], "name": item["name"], "kind": item["kind"], "text": content}
                for item in file_content_rows
            ]
            relation_rows.extend(build_relations(path, content, symbols))
        self._rebuild_subsystems(conn, file_rows, symbol_rows)
        self._rebuild_relations(conn, relation_rows)

    def _rebuild_subsystems(self, conn, file_rows: list[dict], symbol_rows: list[dict]) -> None:
        conn.execute("DELETE FROM subsystems")
        for subsystem in derive_subsystems(file_rows, symbol_rows):
            conn.execute(
                "INSERT INTO subsystems(name, summary, representative_paths, file_count, symbol_count) VALUES(?, ?, ?, ?, ?)",
                (
                    subsystem["name"],
                    subsystem["summary"],
                    json.dumps(subsystem["representative_paths"]),
                    subsystem["file_count"],
                    subsystem["symbol_count"],
                ),
            )

    def _rebuild_relations(self, conn, relation_rows: list[dict]) -> None:
        conn.execute("DELETE FROM relations")

        # Build a suffix → full-path map for import resolution.
        # Shorter suffixes stored first so the most specific match wins on lookup.
        suffix_map: dict[str, str] = {}
        for row in conn.execute("SELECT path FROM files ORDER BY length(path)"):
            p = row["path"]
            parts = p.split("/")
            for i in range(len(parts)):
                suffix = "/".join(parts[i:])
                if suffix not in suffix_map:
                    suffix_map[suffix] = p

        for relation in relation_rows:
            target = relation["target_ref"]
            if target.startswith("import:"):
                source_path = relation["source_ref"].removeprefix("file:")
                resolved = self._resolve_import(target[7:], source_path, suffix_map)
                if resolved:
                    relation = {**relation, "target_ref": resolved}
            conn.execute(
                "INSERT OR IGNORE INTO relations(source_ref, target_ref, relation_type, weight)"
                " VALUES(?, ?, ?, ?)",
                (
                    relation["source_ref"],
                    relation["target_ref"],
                    relation["relation_type"],
                    relation["weight"],
                ),
            )

    @staticmethod
    def _resolve_import(import_path: str, source_file_path: str, suffix_map: dict[str, str]) -> str | None:
        """Attempt to resolve an unresolved import path to a file: ref."""
        # Resolve relative paths (./utils, ../models/user, /utils from Python dot-imports)
        if import_path.startswith("./") or import_path.startswith("../") or import_path.startswith("/"):
            base_dir = source_file_path.rsplit("/", 1)[0] if "/" in source_file_path else ""
            raw = base_dir + "/" + import_path.lstrip("/")
            parts: list[str] = []
            for part in raw.split("/"):
                if part == "..":
                    if parts:
                        parts.pop()
                elif part and part != ".":
                    parts.append(part)
            import_path = "/".join(parts)

        exts = ("", ".py", ".ts", ".tsx", ".js", ".jsx", ".php", ".rb", ".go")
        index_exts = (".ts", ".tsx", ".js", ".jsx")

        # Build candidate list: full path first, then progressively shorter suffixes.
        # Shorter suffixes handle namespace ≠ filesystem root (e.g. PHP App\ vs app/).
        path_parts = import_path.split("/")
        suffixes = ["/".join(path_parts[i:]) for i in range(len(path_parts))]

        candidates: list[str] = []
        for suffix in suffixes:
            for ext in exts:
                candidates.append(suffix + ext)
            for ext in index_exts:
                candidates.append(suffix + "/index" + ext)

        for candidate in candidates:
            if candidate in suffix_map:
                return f"file:{suffix_map[candidate]}"

        return None

    def _get_meta(self, conn, key: str) -> str | None:
        row = conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, conn, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO index_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _overview_summary(
        self,
        repo_name: str,
        total_files: int,
        languages: Counter,
        roles: Counter,
        key_files: list[str],
        top_dirs: Counter,
    ) -> str:
        lang_text = ", ".join(f"{name} ({count})" for name, count in languages.most_common(3)) or "no detected languages"
        role_text = ", ".join(f"{name} ({count})" for name, count in roles.most_common(3)) or "no roles detected"
        dir_text = ", ".join(f"{name} ({count})" for name, count in top_dirs.most_common(5))
        key_text = ", ".join(key_files) if key_files else "none"
        return (
            f"Repository {repo_name} contains {total_files} indexed files. "
            f"Top languages: {lang_text}. Top roles: {role_text}. "
            f"Primary directories: {dir_text}. Key root files: {key_text}."
        )

    def _rebuild_embeddings(self, conn, full: bool = False) -> None:
        """Populate the embeddings table from files, symbols, and subsystems.

        Skips refs whose content_hash already matches the stored row.
        When *full* is True, stale refs (no longer in the index) are pruned first.
        """
        if not embedder.available():
            return

        model_name = embedder.DEFAULT_MODEL

        # Collect all indexable (ref, text, content_hash) triples.
        candidates: list[tuple[str, str, str]] = []
        for row in conn.execute("SELECT path, summary, hash FROM files"):
            candidates.append((f'file:{row["path"]}', row["summary"], row["hash"]))
        for row in conn.execute("SELECT symbol_id, summary, hash FROM symbols"):
            candidates.append((row["symbol_id"], row["summary"], row["hash"]))
        for row in conn.execute("SELECT name, summary FROM subsystems"):
            h = sha256_bytes(row["summary"].encode("utf-8"))
            candidates.append((f'subsystem:{row["name"]}', row["summary"], h))

        if not candidates:
            return

        # Determine which refs need (re-)embedding.
        existing: dict[str, str] = {
            row["ref"]: row["content_hash"]
            for row in conn.execute(
                "SELECT ref, content_hash FROM embeddings WHERE model = ?", (model_name,)
            )
        }
        pending = [(ref, text, h) for ref, text, h in candidates if existing.get(ref) != h]

        if pending:
            refs = [r for r, _, _ in pending]
            texts = [t for _, t, _ in pending]
            hashes = [h for _, _, h in pending]
            vectors = embedder.encode(texts, model_name)
            now = self._now()
            conn.executemany(
                "INSERT OR REPLACE INTO embeddings(ref, vector, model, content_hash, created_at) VALUES (?,?,?,?,?)",
                [(ref, vec, model_name, h, now) for ref, vec, h in zip(refs, vectors, hashes)],
            )

        if full:
            valid_refs = {ref for ref, _, _ in candidates}
            stale = [row["ref"] for row in conn.execute("SELECT ref FROM embeddings") if row["ref"] not in valid_refs]
            for ref in stale:
                conn.execute("DELETE FROM embeddings WHERE ref = ?", (ref,))

    def _semantic_scores(self, conn, query: str) -> dict[str, float]:
        """Return normalized cosine similarity scores keyed by ref string.

        Returns an empty dict when the embeddings extra is not installed or the
        embeddings table is empty.
        """
        if not embedder.available():
            return {}
        model_name = embedder.DEFAULT_MODEL
        query_vecs = embedder.encode([query], model_name)
        if not query_vecs:
            return {}
        ref_vectors = [
            (row["ref"], row["vector"])
            for row in conn.execute("SELECT ref, vector FROM embeddings WHERE model = ?", (model_name,))
        ]
        if not ref_vectors:
            return {}
        raw = embedder.cosine_scores(query_vecs[0], ref_vectors)
        if not raw:
            return {}
        max_score = max(raw.values())
        if max_score <= 0:
            return {}
        return {k: v / max_score for k, v in raw.items()}

    def _fts_scores(self, conn, query: str) -> dict[str, float]:
        """Return normalized BM25 scores from FTS5 tables keyed by ref string."""
        import sqlite3

        q = fts_query(query)
        if not q:
            return {}
        raw: dict[str, float] = {}
        for table, ref_col, ref_prefix in (
            ("fts_files", "path", "file:"),
            ("fts_symbols", "symbol_id", ""),
        ):
            try:
                for row in conn.execute(
                    f"SELECT {ref_col}, bm25({table}) AS score FROM {table} WHERE {table} MATCH ?",
                    (q,),
                ):
                    ref = f"{ref_prefix}{row[ref_col]}" if ref_prefix else row[ref_col]
                    raw[ref] = -row["score"]  # bm25() returns negative; negate to get positive
            except sqlite3.OperationalError:
                pass
        if not raw:
            return {}
        max_score = max(raw.values())
        if max_score <= 0:
            return {}
        return {k: v / max_score for k, v in raw.items()}

    def _search_cache_key(self, query: str, kind: str | None, limit: int) -> str:
        return sha256_bytes(f"search:{query}|{kind or ''}|{limit}".encode("utf-8"))

    def _file_score(self, path: str, summary: str, role: str, language: str, query_terms: Counter[str]) -> float:
        exact = 0.0
        haystacks = [path.lower(), summary.lower(), role.lower(), language.lower()]
        for token, count in query_terms.items():
            for haystack in haystacks:
                if token in haystack:
                    exact += 1.0 * count
            if any(part == token for part in path.lower().split("/")):
                exact += 1.5 * count
        semantic = cosineish_score(query_terms, f"{path} {summary} {role} {language}")
        return exact + semantic

    def _symbol_score(self, path: str, name: str, kind: str, summary: str, query_terms: Counter[str]) -> float:
        exact = 0.0
        text = f"{path} {name} {kind} {summary}".lower()
        for token, count in query_terms.items():
            if token == name.lower():
                exact += 2.5 * count
            elif token in text:
                exact += 1.0 * count
        semantic = cosineish_score(query_terms, text)
        return exact + semantic

    def _expand_related_results(self, conn, search_results: list[dict]) -> list[dict]:
        expanded = list(search_results)
        refs = [row["ref"] for row in search_results[:4]]
        for ref in refs:
            for relation in conn.execute(
                "SELECT source_ref, target_ref, relation_type, weight FROM relations WHERE source_ref = ? OR target_ref = ? ORDER BY weight DESC",
                (ref, ref),
            ):
                neighbor = relation["target_ref"] if relation["source_ref"] == ref else relation["source_ref"]
                expanded.append(
                    {
                        "ref": neighbor,
                        "path": neighbor.split(":", 2)[-1],
                        "name": neighbor.split(":")[-1],
                        "kind": "related",
                        "role": "related",
                        "language": "related",
                        "score": relation["weight"],
                        "summary": f'Related via {relation["relation_type"]}',
                    }
                )
        deduped = []
        seen: set[str] = set()
        for item in sorted(expanded, key=lambda row: (-row["score"], row["ref"])):
            if item["ref"] in seen:
                continue
            seen.add(item["ref"])
            deduped.append(item)
        return deduped

    def _materialize_result(self, conn, result: dict) -> dict | None:
        ref = result["ref"]
        if ref.startswith("symbol:"):
            row = conn.execute(
                "SELECT symbol_id, path, name, kind, signature, summary FROM symbols WHERE symbol_id = ?",
                (ref,),
            ).fetchone()
            if row is None:
                return None
            chunk_rows = conn.execute(
                "SELECT chunk_id, path, line_start, line_end, text, summary, token_estimate FROM chunks WHERE symbol_id = ? ORDER BY line_start",
                (ref,),
            ).fetchall()
            chunks = [self._chunk_from_row(row) for row in chunk_rows]
            summary = SymbolSummary(
                symbol_id=row["symbol_id"],
                path=row["path"],
                name=row["name"],
                kind=row["kind"],
                signature=row["signature"],
                summary=row["summary"],
            )
            return {
                "kind": "symbol",
                "summary": row["summary"],
                "summary_payload": summary.to_dict(),
                "chunks": chunks,
            }

        if ref.startswith("subsystem:"):
            row = conn.execute("SELECT summary, representative_paths FROM subsystems WHERE name = ?", (ref.split(":", 1)[1],)).fetchone()
            if row is None:
                return None
            paths = json.loads(row["representative_paths"])
            chunk_rows = conn.execute(
                "SELECT chunk_id, path, line_start, line_end, text, summary, token_estimate FROM chunks WHERE path IN ({}) ORDER BY token_estimate ASC LIMIT 2".format(
                    ",".join("?" for _ in paths)
                ),
                tuple(paths),
            ).fetchall() if paths else []
            return {
                "kind": "subsystem",
                "summary": row["summary"],
                "summary_payload": row["summary"],
                "chunks": [self._chunk_from_row(item) for item in chunk_rows],
            }

        if ref.startswith("file:"):
            path = ref.split(":", 1)[1]
        else:
            path = result["path"]
        row = conn.execute("SELECT summary FROM files WHERE path = ?", (path,)).fetchone()
        if row is None:
            return None
        chunk_rows = conn.execute(
            "SELECT chunk_id, path, line_start, line_end, text, summary, token_estimate FROM chunks WHERE path = ? ORDER BY token_estimate ASC, line_start ASC LIMIT 2",
            (path,),
        ).fetchall()
        return {
            "kind": "file",
            "summary": f"{path}: {row['summary']}",
            "summary_payload": f"{path}: {row['summary']}",
            "chunks": [self._chunk_from_row(item) for item in chunk_rows],
        }

    def _chunk_from_row(self, row) -> CodeChunk:
        return CodeChunk(
            chunk_id=row["chunk_id"],
            path=row["path"],
            line_start=row["line_start"],
            line_end=row["line_end"],
            text=row["text"],
            summary=row["summary"],
            token_estimate=row["token_estimate"],
        )

    def _relevant_memories(self, conn, query: str) -> list[MemoryEntry]:
        query_terms = query_vector(query)
        rows = conn.execute(
            "SELECT memory_id, title, summary, evidence_refs, evidence_hash, created_at, last_validated_at"
            " FROM memories ORDER BY memory_id DESC"
        ).fetchall()
        fresh: list[tuple[float, MemoryEntry]] = []
        stale: list[tuple[float, MemoryEntry]] = []
        for row in rows:
            is_stale = row["evidence_hash"] != self._evidence_hash(conn, json.loads(row["evidence_refs"]))
            score = cosineish_score(query_terms, f'{row["title"]} {row["summary"]}')
            if score <= 0 and query_terms:
                continue

            validated_at = row["last_validated_at"]
            if not is_stale:
                validated_at = self._now()
                conn.execute(
                    "UPDATE memories SET last_validated_at = ? WHERE memory_id = ?",
                    (validated_at, row["memory_id"]),
                )

            entry = MemoryEntry(
                memory_id=row["memory_id"],
                title=row["title"],
                summary=row["summary"],
                evidence_refs=json.loads(row["evidence_refs"]),
                created_at=row["created_at"],
                stale=is_stale,
                last_validated_at=validated_at,
            )
            (stale if is_stale else fresh).append((score, entry))

        fresh.sort(key=lambda x: (-x[0], -int(x[1].memory_id or 0)))
        stale.sort(key=lambda x: (-x[0], -int(x[1].memory_id or 0)))

        # Return up to 3 fresh memories; only fall back to stale when none are fresh.
        results = [e for _, e in fresh[:3]]
        if not results:
            results = [e for _, e in stale[:1]]
        return results

    def _compose_explanation(self, subject: str, search_results: list[dict], subsystems: list[str]) -> str:
        if not search_results:
            return f"No indexed files matched {subject!r}. Refresh or index the repository before asking again."
        refs = ", ".join(item["ref"] for item in search_results[:4])
        subsystem = subsystems[0] if subsystems else "No subsystem summary available."
        return f"{subject!r} is most closely connected to {refs}. Top subsystem context: {subsystem}"

    def _evidence_hash(self, conn, evidence_refs: list[str]) -> str:
        parts = []
        for ref in evidence_refs:
            if ref.startswith("symbol:"):
                row = conn.execute("SELECT hash FROM symbols WHERE symbol_id = ?", (ref,)).fetchone()
                parts.append(row["hash"] if row else "missing")
            elif ref.startswith("chunk:"):
                chunk_id = ref.removeprefix("chunk:")
                row = conn.execute("SELECT hash FROM chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()
                parts.append(row["hash"] if row else "missing")
            else:
                path = ref.removeprefix("file:")
                row = conn.execute("SELECT hash FROM files WHERE path = ?", (path,)).fetchone()
                parts.append(row["hash"] if row else "missing")
        return sha256_bytes("|".join(parts).encode("utf-8"))

    def _cache_key(self, query: str, budget_tokens: int, scope: str | None) -> str:
        return sha256_bytes(f"{query}|{budget_tokens}|{scope or ''}".encode("utf-8"))

    def _get_cached_query(self, conn, query_key: str) -> dict | None:
        row = conn.execute("SELECT response_json FROM query_cache WHERE query_key = ?", (query_key,)).fetchone()
        return json.loads(row["response_json"]) if row else None

    def _store_cached_query(self, conn, query_key: str, query_text: str, payload: dict) -> None:
        conn.execute(
            "INSERT INTO query_cache(query_key, query_text, response_json, created_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(query_key) DO UPDATE SET query_text=excluded.query_text, response_json=excluded.response_json, created_at=excluded.created_at",
            (query_key, query_text, json.dumps(payload), self._now()),
        )
        total = conn.execute("SELECT COUNT(*) AS count FROM query_cache").fetchone()["count"]
        if total > QUERY_CACHE_LIMIT:
            conn.execute(
                "DELETE FROM query_cache WHERE query_key IN (SELECT query_key FROM query_cache ORDER BY created_at ASC LIMIT ?)",
                (total - QUERY_CACHE_LIMIT,),
            )

    def _clear_query_cache(self, conn) -> None:
        conn.execute("DELETE FROM query_cache")

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()
