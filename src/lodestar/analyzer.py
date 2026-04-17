"""Heuristic analyzers for files, symbols, subsystems, and relations."""

from __future__ import annotations

import ast
import re
from collections import Counter, defaultdict

from .config import CHUNK_OVERLAP_LINES, CHUNK_SIZE_LINES
from .utils import sha256_bytes, token_estimate
from . import parsers as _parsers


IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z0-9_./]+)", re.MULTILINE)
# JS/TS: import ... from '...' / export ... from '...' / require('...')
JS_IMPORT_RE = re.compile(
    r'(?:import|export)[^"\']*["\']([^"\']+)["\']'
    r'|(?:require|import)\s*\(\s*["\']([^"\']+)["\']\s*\)',
    re.MULTILINE,
)
# PHP: use App\Models\User; (captures the fully-qualified class path)
PHP_USE_RE = re.compile(r"^\s*use\s+([\w\\]+(?:\\[\w]+)*)\s*;", re.MULTILINE)
JS_SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
GENERIC_SECTION_RE = re.compile(r"^(#+\s+.+|[A-Z][A-Za-z0-9 _-]{2,}:)\s*$", re.MULTILINE)


def build_chunks(rel_path: str, content: str) -> list[dict]:
    lines = content.splitlines()
    if not lines:
        return []
    chunks: list[dict] = []
    step = max(1, CHUNK_SIZE_LINES - CHUNK_OVERLAP_LINES)
    for start_index in range(0, len(lines), step):
        end_index = min(len(lines), start_index + CHUNK_SIZE_LINES)
        text = "\n".join(lines[start_index:end_index]).strip()
        if not text:
            continue
        chunks.append(_chunk_payload(rel_path, text, start_index + 1, end_index, "window"))
        if end_index == len(lines):
            break
    return chunks


def extract_symbols(rel_path: str, language: str, content: str) -> list[dict]:
    ts_result = _parsers.extract_symbols(rel_path, language, content)
    if ts_result is not None:
        return ts_result
    if language == "python":
        return _extract_python_symbols(rel_path, content)
    if language in {"javascript", "typescript"}:
        return _extract_js_like_symbols(rel_path, content)
    return _extract_generic_sections(rel_path, content)


def build_relations(path: str, content: str, symbols: list[dict]) -> list[dict]:
    relations: list[dict] = []
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""

    if ext in {"js", "jsx", "ts", "tsx"}:
        imported = set()
        for m in JS_IMPORT_RE.finditer(content):
            raw = m.group(1) or m.group(2)
            if raw and not raw.startswith("http"):
                imported.add(raw)
    elif ext == "php":
        imported = {
            m.group(1).replace("\\", "/")
            for m in PHP_USE_RE.finditer(content)
        }
    else:
        # Python and generic: capture module path, normalise dots to slashes
        imported = {
            m.group(1).replace(".", "/")
            for m in IMPORT_RE.finditer(content)
            if m.group(1) and not m.group(1).startswith(".")
        }
        # Relative Python imports: .utils → ./utils, ..models.user → ../models/user
        for m in IMPORT_RE.finditer(content):
            raw = m.group(1)
            if not raw or not raw.startswith("."):
                continue
            leading = len(raw) - len(raw.lstrip("."))
            rest = raw.lstrip(".").replace(".", "/")
            prefix = "./" if leading == 1 else "../" * (leading - 1)
            imported.add(prefix + rest if rest else prefix.rstrip("/"))


    for target in sorted(imported):
        relations.append(
            {
                "source_ref": f"file:{path}",
                "target_ref": f"import:{target}",
                "relation_type": "imports",
                "weight": 1.0,
            }
        )

    name_to_symbol_ids = defaultdict(list)
    for symbol in symbols:
        name_to_symbol_ids[symbol["name"]].append(symbol["symbol_id"])

    if name_to_symbol_ids:
        # Compile ONE combined pattern for all candidate names instead of N² dynamic patterns.
        combined = re.compile(
            r"\b(" + "|".join(re.escape(n) for n in name_to_symbol_ids) + r")\b"
        )
        for symbol in symbols:
            matches = set(combined.findall(symbol["text"]))
            for match_name in matches:
                if match_name == symbol["name"]:
                    continue
                for target_id in name_to_symbol_ids[match_name][:1]:
                    relations.append(
                        {
                            "source_ref": symbol["symbol_id"],
                            "target_ref": target_id,
                            "relation_type": "references",
                            "weight": 0.6,
                        }
                    )
    return relations


def derive_subsystems(file_rows: list[dict], symbol_rows: list[dict]) -> list[dict]:
    file_groups: dict[str, list[dict]] = defaultdict(list)
    symbol_counts: Counter[str] = Counter()

    for row in file_rows:
        subsystem = subsystem_name_for_path(row["path"])
        file_groups[subsystem].append(row)
    for row in symbol_rows:
        subsystem = subsystem_name_for_path(row["path"])
        symbol_counts[subsystem] += 1

    payloads: list[dict] = []
    for name, rows in sorted(file_groups.items()):
        languages = Counter(item["language"] for item in rows)
        roles = Counter(item["role"] for item in rows)
        representative = rows[0]["summary"] if rows else "No summary available."
        summary = (
            f"{name} subsystem has {len(rows)} files and {symbol_counts[name]} symbols. "
            f"Top languages: {', '.join(f'{lang} ({count})' for lang, count in languages.most_common(2)) or 'none'}. "
            f"Top roles: {', '.join(f'{role} ({count})' for role, count in roles.most_common(2)) or 'none'}. "
            f"Representative file: {representative}"
        )
        payloads.append(
            {
                "name": name,
                "summary": summary,
                "representative_paths": [row["path"] for row in rows[:4]],
                "file_count": len(rows),
                "symbol_count": symbol_counts[name],
            }
        )
    return payloads


def subsystem_name_for_path(path: str) -> str:
    top = path.split("/", 1)[0] if "/" in path else "root"
    name = top.lower()
    if name in {"src", "app", "lib"}:
        return "application"
    if name in {"tests", "test"}:
        return "tests"
    if name in {"docs", "doc"}:
        return "documentation"
    if name in {"scripts", "bin"}:
        return "scripts"
    return top


def query_vector(text: str) -> Counter[str]:
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_/-]+", text.lower())
    return Counter(word for word in words if len(word) >= 2)


def fts_query(text: str) -> str:
    """Convert a natural language query string to FTS5 MATCH syntax (OR of tokens)."""
    terms = re.findall(r"[a-zA-Z][a-zA-Z0-9]*", text.lower())
    if not terms:
        return ""
    return " OR ".join(terms)


def cosineish_score(query_terms: Counter[str], text: str) -> float:
    if not query_terms:
        return 0.0
    candidate_terms = query_vector(text)
    if not candidate_terms:
        return 0.0
    overlap = sum(min(count, candidate_terms.get(term, 0)) for term, count in query_terms.items())
    return overlap / max(sum(query_terms.values()), 1)


def fts_find_symbol_refs(conn, symbol_name: str, exclude_path: str | None = None) -> list[dict]:
    """Search FTS chunks for whole-word occurrences of *symbol_name*.

    Returns a list of ``{path, line_start, line_end, context}`` dicts for each
    chunk that contains the symbol name as a whole word, excluding the file
    where the symbol is defined (*exclude_path*).
    """
    import sqlite3

    fts_term = fts_query(symbol_name)
    if not fts_term:
        return []

    boundary = re.compile(r"\b" + re.escape(symbol_name) + r"\b")
    hits: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT c.path, c.line_start, c.line_end, c.text "
            "FROM fts_chunks fc "
            "JOIN chunks c ON fc.chunk_id = c.chunk_id "
            "WHERE fts_chunks MATCH ?",
            (fts_term,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    for row in rows:
        if exclude_path and row["path"] == exclude_path:
            continue
        if not boundary.search(row["text"]):
            continue
        # Extract the first matching line as context
        for line in row["text"].splitlines():
            if boundary.search(line):
                context = line.strip()[:160]
                break
        else:
            context = row["text"].splitlines()[0].strip()[:160]
        hits.append({
            "path": row["path"],
            "line_start": row["line_start"],
            "line_end": row["line_end"],
            "context": context,
        })
    return hits


def _extract_python_symbols(rel_path: str, content: str) -> list[dict]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return _extract_generic_sections(rel_path, content)

    lines = content.splitlines()
    symbols: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        end_lineno = getattr(node, "end_lineno", node.lineno)
        text = "\n".join(lines[node.lineno - 1 : end_lineno])
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        signature = _python_signature(node)
        summary = f"{kind} {node.name} in {rel_path} at lines {node.lineno}-{end_lineno}"
        symbols.append(
            {
                "symbol_id": f"symbol:{rel_path}:{node.name}:{node.lineno}",
                "path": rel_path,
                "name": node.name,
                "kind": kind,
                "signature": signature,
                "line_start": node.lineno,
                "line_end": end_lineno,
                "summary": summary,
                "text": text,
                "token_estimate": token_estimate(text),
            }
        )
    return sorted(symbols, key=lambda item: (item["line_start"], item["name"]))


def _extract_js_like_symbols(rel_path: str, content: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    for match in JS_SYMBOL_RE.finditer(content):
        name = match.group(1)
        line_start = content[: match.start()].count("\n") + 1
        line_end = min(len(lines), line_start + CHUNK_SIZE_LINES - 1)
        text = "\n".join(lines[line_start - 1 : line_end])
        symbols.append(
            {
                "symbol_id": f"symbol:{rel_path}:{name}:{line_start}",
                "path": rel_path,
                "name": name,
                "kind": "symbol",
                "signature": lines[line_start - 1].strip()[:160],
                "line_start": line_start,
                "line_end": line_end,
                "summary": f"symbol {name} in {rel_path} at lines {line_start}-{line_end}",
                "text": text,
                "token_estimate": token_estimate(text),
            }
        )
    return symbols


def _extract_generic_sections(rel_path: str, content: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    for match in GENERIC_SECTION_RE.finditer(content):
        heading = match.group(1).strip().lstrip("#").strip()
        line_start = content[: match.start()].count("\n") + 1
        line_end = min(len(lines), line_start + CHUNK_SIZE_LINES - 1)
        text = "\n".join(lines[line_start - 1 : line_end])
        symbol_id = f"symbol:{rel_path}:{heading}:{line_start}"
        symbols.append(
            {
                "symbol_id": symbol_id,
                "path": rel_path,
                "name": heading,
                "kind": "section",
                "signature": heading,
                "line_start": line_start,
                "line_end": line_end,
                "summary": f"section {heading} in {rel_path} at lines {line_start}-{line_end}",
                "text": text,
                "token_estimate": token_estimate(text),
            }
        )
    return symbols


def _chunk_payload(rel_path: str, text: str, line_start: int, line_end: int, chunk_type: str) -> dict:
    chunk_id = f"{rel_path}:{line_start}-{line_end}"
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return {
        "chunk_id": chunk_id,
        "line_start": line_start,
        "line_end": line_end,
        "text": text,
        "summary": first_line[:160] if first_line else "Chunk of repository text.",
        "token_estimate": token_estimate(text),
        "hash": sha256_bytes(text.encode("utf-8")),
        "chunk_type": chunk_type,
    }


def _python_signature(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        return f"class {node.name}"
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ""
    args = [arg.arg for arg in node.args.args]
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({', '.join(args)})"
