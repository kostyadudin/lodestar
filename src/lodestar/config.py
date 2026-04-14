"""Static configuration for Lodestar."""

from pathlib import Path

APP_NAME = "lodestar"
STATE_DIRNAME = ".lodestar"
DB_FILENAME = "index.db"
VERSION_FILENAME = "version.json"
REPO_CONFIG_FILENAME = "config.json"

DEFAULT_BUDGET_TOKENS = 1800
DEFAULT_LIMIT = 8
CHUNK_SIZE_LINES = 40
CHUNK_OVERLAP_LINES = 8
MAX_FILE_BYTES = 512 * 1024
QUERY_CACHE_LIMIT = 100

EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".lodestar",
    ".venv",
    "venv",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "coverage",
    ".pytest_cache",
    ".mypy_cache",
    "__pycache__",
    "target",
    ".next",
    ".turbo",
    ".idea",
    "storage",
}

EXCLUDED_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".dll",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".mp4",
    ".mov",
}

TEXT_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".md",
    ".txt",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".swift",
    ".rb",
    ".php",
    ".html",
    ".css",
    ".scss",
    ".sql",
    ".sh",
}

ROLE_HINTS = {
    "README.md": "documentation",
    "AGENTS.md": "agent-guidance",
    "CLAUDE.md": "agent-guidance",
    "pyproject.toml": "build-config",
    "package.json": "build-config",
    "Cargo.toml": "build-config",
    "go.mod": "build-config",
    "docker-compose.yml": "runtime-config",
    "docker-compose.yaml": "runtime-config",
    "Dockerfile": "runtime-config",
}

# Filenames that are application entry points across ecosystems.
ENTRYPOINT_NAMES = {
    "main.py", "app.py", "server.py", "wsgi.py", "asgi.py",
    "manage.py",
    "index.js", "server.js", "app.js",
    "index.ts", "server.ts", "app.ts",
    "index.php", "app.php",
    "main.go",
    "main.rs",
    "Application.java",
}

LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".json": "json",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".md": "markdown",
    ".txt": "text",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".rb": "ruby",
    ".php": "php",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".sql": "sql",
    ".sh": "shell",
}

ROOT_FILES = {
    "README.md",
    "AGENTS.md",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
}


def state_path(repo_root: Path) -> Path:
    return repo_root / STATE_DIRNAME
