"""Typed structures used by Lodestar."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class SearchResult:
    ref: str
    path: str
    name: str
    kind: str
    role: str
    language: str
    score: float
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EvidenceRef:
    path: str
    chunk_id: str | None = None
    line_start: int | None = None
    line_end: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CodeChunk:
    chunk_id: str
    path: str
    line_start: int
    line_end: int
    text: str
    summary: str
    token_estimate: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MemoryEntry:
    memory_id: int | None
    title: str
    summary: str
    evidence_refs: list[str]
    created_at: str
    stale: bool = False
    last_validated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SymbolSummary:
    symbol_id: str
    path: str
    name: str
    kind: str
    signature: str
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ContextPack:
    repo_summary: str
    subsystem_summaries: list[str] = field(default_factory=list)
    symbol_summaries: list[dict[str, Any] | str] = field(default_factory=list)
    code_chunks: list[CodeChunk] = field(default_factory=list)
    memories: list[MemoryEntry] = field(default_factory=list)
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    token_estimate: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_summary": self.repo_summary,
            "subsystem_summaries": self.subsystem_summaries,
            "symbol_summaries": self.symbol_summaries,
            "code_chunks": [item.to_dict() for item in self.code_chunks],
            "memories": [item.to_dict() for item in self.memories],
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
            "token_estimate": self.token_estimate,
        }
