"""Per-repository configuration loaded from .lodestar/config.json."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import DEFAULT_BUDGET_TOKENS, DEFAULT_LIMIT, REPO_CONFIG_FILENAME


@dataclass
class RepoConfig:
    """Repo-local policy overrides for indexing and retrieval."""

    # Extra directory names to exclude (same semantics as EXCLUDED_DIRS)
    extra_excludes: set[str] = field(default_factory=set)
    # Glob patterns (relative path) that bypass all exclusion rules
    include_overrides: list[str] = field(default_factory=list)
    # Glob pattern → role string overrides applied before heuristics
    role_overrides: dict[str, str] = field(default_factory=dict)
    # Language name → enabled bool; False disables symbol extraction for that language
    parser_overrides: dict[str, bool] = field(default_factory=dict)
    # Optional overrides for budget_tokens and limit used by retrieval methods
    retrieval_defaults: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_state(cls, state_path: Path) -> RepoConfig:
        config_file = state_path / REPO_CONFIG_FILENAME
        if not config_file.exists():
            return cls()
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        return cls(
            extra_excludes=set(data.get("extra_excludes", [])),
            include_overrides=list(data.get("include_overrides", [])),
            role_overrides=dict(data.get("role_overrides", {})),
            parser_overrides={k: bool(v) for k, v in data.get("parser_overrides", {}).items()},
            retrieval_defaults=dict(data.get("retrieval_defaults", {})),
        )

    def is_excluded(self, rel_parts: tuple[str, ...]) -> bool:
        """Return True if any non-filename path component is in extra_excludes."""
        return any(part in self.extra_excludes for part in rel_parts[:-1])

    def is_force_included(self, rel_path: str) -> bool:
        """Return True if the path matches an include_override glob (bypasses all exclusions)."""
        return any(fnmatch.fnmatch(rel_path, pattern) for pattern in self.include_overrides)

    def role_for(self, rel_path: str) -> str | None:
        """Return the overridden role for this path, or None if no override matches."""
        for pattern, role in self.role_overrides.items():
            if fnmatch.fnmatch(rel_path, pattern):
                return role
        return None

    def parser_enabled(self, language: str) -> bool:
        """Return False if symbol extraction is disabled for this language."""
        return self.parser_overrides.get(language, True)

    def effective_limit(self, limit: int | None) -> int:
        if limit is not None:
            return limit
        return self.retrieval_defaults.get("limit", DEFAULT_LIMIT)

    def effective_budget(self, budget_tokens: int | None) -> int:
        if budget_tokens is not None:
            return budget_tokens
        return self.retrieval_defaults.get("budget_tokens", DEFAULT_BUDGET_TOKENS)
