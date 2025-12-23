"""Work/personal session detector with configurable patterns."""

import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "claudeboss"
PATTERNS_FILE = CONFIG_DIR / "patterns.json"

# Default work indicators
DEFAULT_WORK_PATTERNS = ["work", "company", "client", "project"]


def _load_patterns() -> list[str]:
    """Load work patterns from config or use defaults."""
    if PATTERNS_FILE.exists():
        try:
            with open(PATTERNS_FILE) as f:
                config = json.load(f)
                return config.get("work_patterns", DEFAULT_WORK_PATTERNS)
        except (json.JSONDecodeError, IOError):
            pass
    return DEFAULT_WORK_PATTERNS


def is_work_session(cwd: str, project_path: str, first_message: str = "", summary: str = "") -> bool:
    """Check if a session is work-related."""
    patterns = _load_patterns()
    text = f"{cwd} {project_path}".lower()
    return any(p.lower() in text for p in patterns)
