"""Capture transcripts into the memory store.

Two input formats:

* `json`        — list of `{title, summary, evidence_refs?}` records (passthrough,
                  validation only).
* `claude-jsonl` — official Claude Code session log; one memory per session,
                   heuristically summarising touched files and the first user
                   prompt.

No LLM is invoked. Default mode is dry-run; pass `commit=True` to write.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from .indexer import LodestarService


CLAUDE_FILE_FIELDS = ("file_path", "path", "notebook_path")
MAX_SUMMARY_CHARS = 1000
MAX_TOUCHED_FILES_IN_SUMMARY = 8


def capture(
    service: "LodestarService",
    repo_root: str,
    source: str,
    input_format: str,
    commit: bool = False,
) -> dict[str, Any]:
    """Ingest *source* in *input_format* and emit proposed memory records.

    Returns a report dict. When *commit* is False, no memories are written.
    """
    records, parse_skips = _load_records(source, input_format)

    written: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = list(parse_skips)

    existing_titles = _existing_titles(service, repo_root) if commit else set()

    for record in records:
        title = (record.get("title") or "").strip()
        summary = (record.get("summary") or "").strip()
        if not title:
            skipped.append({"reason": "missing_title", "context": record})
            continue
        if not summary:
            skipped.append({"reason": "missing_summary", "context": {"title": title}})
            continue
        if commit and title in existing_titles:
            skipped.append({"reason": "duplicate_title", "context": {"title": title}})
            continue
        evidence_refs = [str(r) for r in record.get("evidence_refs") or [] if isinstance(r, str) and r]
        entry: dict[str, Any] = {
            "title": title,
            "summary": summary[:MAX_SUMMARY_CHARS],
            "evidence_refs": evidence_refs,
        }
        if commit:
            payload = service.remember(repo_root, title, entry["summary"], evidence_refs=evidence_refs)
            entry["memory_id"] = payload["memory_id"]
            existing_titles.add(title)
        else:
            entry["memory_id"] = None
        written.append(entry)

    return {
        "repo_root": repo_root,
        "input_format": input_format,
        "source": source,
        "dry_run": not commit,
        "written": written,
        "skipped": skipped,
        "stats": {
            "candidates": len(records),
            "written_count": len(written),
            "skipped_count": len(skipped),
        },
    }


def _load_records(source: str, input_format: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if input_format == "json":
        return _load_json_records(source), []
    if input_format == "claude-jsonl":
        return _load_claude_jsonl(source)
    raise ValueError(f"Unsupported input format: {input_format!r}")


def _load_json_records(source: str) -> list[dict[str, Any]]:
    text = _read_source(source)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    raise ValueError("JSON input must be an object or array of objects.")


def _load_claude_jsonl(source: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    skipped: list[dict[str, Any]] = []
    sessions: dict[str, dict[str, Any]] = {}
    default_session = Path(source).stem if source != "-" else "stdin"

    for line in _iter_jsonl(source):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            skipped.append({"reason": "invalid_jsonl_line", "context": {"error": str(exc)}})
            continue
        if not isinstance(event, dict):
            continue

        sid = event.get("sessionId") or event.get("session_id") or default_session
        session = sessions.setdefault(
            sid,
            {
                "session_id": sid,
                "first_user_text": None,
                "first_assistant_text": None,
                "files": set(),
                "earliest_ts": None,
                "latest_ts": None,
            },
        )
        ts = event.get("timestamp") or ""
        if ts:
            if session["earliest_ts"] is None or ts < session["earliest_ts"]:
                session["earliest_ts"] = ts
            if session["latest_ts"] is None or ts > session["latest_ts"]:
                session["latest_ts"] = ts

        kind = event.get("type")
        message = event.get("message") if isinstance(event.get("message"), dict) else None

        if kind == "user" and message and session["first_user_text"] is None:
            session["first_user_text"] = _extract_user_text(message)
        if kind == "assistant" and message and session["first_assistant_text"] is None:
            session["first_assistant_text"] = _extract_assistant_text(message)

        if message:
            session["files"].update(_extract_files_from_message(message))

    records: list[dict[str, Any]] = []
    for session in sessions.values():
        if not session["files"] and not session["first_user_text"]:
            continue
        records.append(_session_to_record(session))

    return records, skipped


def _session_to_record(session: dict[str, Any]) -> dict[str, Any]:
    files = sorted(session["files"])
    first_user = (session["first_user_text"] or "").strip()
    first_assistant = (session["first_assistant_text"] or "").strip()
    ts = session["earliest_ts"] or session["latest_ts"] or ""
    sid = session["session_id"]

    title_hint = first_user.splitlines()[0][:80] if first_user else f"Session {sid[:8]}"
    title = f"Session {ts[:10]}: {title_hint}" if ts else f"Session {sid[:8]}: {title_hint}"

    files_summary = ", ".join(files[:MAX_TOUCHED_FILES_IN_SUMMARY]) if files else "no files touched"
    if len(files) > MAX_TOUCHED_FILES_IN_SUMMARY:
        files_summary += f" (+{len(files) - MAX_TOUCHED_FILES_IN_SUMMARY} more)"

    parts = [
        f"Session id: {sid}",
        f"Time range: {session['earliest_ts'] or 'unknown'} → {session['latest_ts'] or 'unknown'}",
        f"Touched: {files_summary}",
    ]
    if first_user:
        parts.append(f"User: {first_user[:240]}")
    if first_assistant:
        parts.append(f"Assistant: {first_assistant[:240]}")
    summary = "\n".join(parts)

    evidence_refs = [f"file:{p}" for p in files]
    return {"title": title, "summary": summary, "evidence_refs": evidence_refs}


def _extract_user_text(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                return item["text"]
    return None


def _extract_assistant_text(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                return item["text"]
    return None


def _extract_files_from_message(message: dict[str, Any]) -> set[str]:
    files: set[str] = set()
    content = message.get("content")
    if not isinstance(content, list):
        return files
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "tool_use":
            inp = item.get("input") if isinstance(item.get("input"), dict) else {}
            for field in CLAUDE_FILE_FIELDS:
                value = inp.get(field)
                if isinstance(value, str) and value:
                    files.add(_normalise_path(value))
            command = inp.get("command")
            if isinstance(command, str):
                files.update(_extract_paths_from_command(command))
        elif item.get("type") == "tool_result":
            result_content = item.get("content")
            if isinstance(result_content, str):
                files.update(_extract_paths_from_command(result_content))
    return files


_BARE_PATH_RE = re.compile(r"(?:^|[\s'\"])(\.{0,2}/[\w./_\-]+\.[A-Za-z0-9]+)")


def _extract_paths_from_command(text: str) -> set[str]:
    paths: set[str] = set()
    for match in _BARE_PATH_RE.finditer(text):
        candidate = match.group(1).strip("'\"")
        if "://" in candidate:
            continue
        paths.add(_normalise_path(candidate))
    return paths


def _normalise_path(value: str) -> str:
    """Strip leading './' and absolute prefixes that are unstable across machines."""
    value = value.strip().strip("'\"")
    if value.startswith("./"):
        value = value[2:]
    return value


def _iter_jsonl(source: str) -> Iterable[str]:
    if source == "-":
        for line in sys.stdin:
            line = line.strip()
            if line:
                yield line
        return
    path = Path(source).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"JSONL source not found: {source}")
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield line


def _read_source(source: str) -> str:
    if source == "-":
        return sys.stdin.read()
    path = Path(source).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Source not found: {source}")
    return path.read_text(encoding="utf-8")


def _existing_titles(service: "LodestarService", repo_root: str) -> set[str]:
    """Return the set of currently-stored memory titles to suppress duplicates."""
    from .config import DB_FILENAME
    from .storage import connect

    root = service._repo_root(repo_root)
    state = service._ensure_state(root)
    conn = connect(state / DB_FILENAME)
    return {row["title"] for row in conn.execute("SELECT title FROM memories")}
