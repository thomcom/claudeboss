"""Session data model and loader for Claude Code sessions."""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class Session:
    """Represents a Claude Code session."""

    uuid: str
    slug: str
    cwd: str
    project_path: str  # The encoded project path like -home-devkit-nv
    mtime: datetime
    context_size: int  # File size in bytes as proxy for context
    summary: str = ""
    git_branch: str = ""
    model: str = ""
    first_message: str = ""
    last_summary: str = ""  # 3-10 word summary of recent activity
    context_start: str = ""  # First ~500 tokens for summarization
    context_end: str = ""  # Last ~500 tokens for summarization

    # Computed
    category: str = field(default="personal")  # personal, professional
    is_active: bool = field(default=False)  # True if session is currently running

    @property
    def display_path(self) -> str:
        """Human-readable project path."""
        return self.cwd if self.cwd else self.project_path.replace("-", "/")

    @property
    def short_path(self) -> str:
        """Shortened path for display."""
        path = self.display_path
        home = os.path.expanduser("~")
        if path.startswith(home):
            path = "~" + path[len(home):]
        # Truncate long paths
        if len(path) > 40:
            parts = path.split("/")
            if len(parts) > 3:
                path = "/".join(parts[:2]) + "/.../" + parts[-1]
        return path

    @property
    def dir_name(self) -> str:
        """Leaf directory name for compact display."""
        path = self.display_path
        parts = path.rstrip("/").split("/")
        return parts[-1] if parts else path

    @property
    def mtime_display(self) -> str:
        """Human-readable modification time."""
        now = datetime.now()
        diff = now - self.mtime

        if diff.days == 0:
            hours = diff.seconds // 3600
            if hours == 0:
                mins = diff.seconds // 60
                return f"{mins}m ago"
            return f"{hours}h ago"
        elif diff.days == 1:
            return "yesterday"
        elif diff.days < 7:
            return f"{diff.days}d ago"
        else:
            return self.mtime.strftime("%b %d")

    @property
    def context_display(self) -> str:
        """Human-readable context size."""
        size = self.context_size
        if size < 1024:
            return f"{size}B"
        elif size < 1024 * 1024:
            return f"{size // 1024}KB"
        else:
            return f"{size // (1024 * 1024)}MB"


def categorize_session(session: Session) -> str:
    """Determine if session is personal or professional using learned profile."""
    # Import here to avoid circular dependency
    from .detector import is_work_session

    if is_work_session(
        session.cwd,
        session.project_path,
        session.first_message,
        session.summary,
    ):
        return "professional"

    return "personal"


def load_sessions(claude_dir: Optional[str] = None) -> list[Session]:
    """Load all sessions from Claude Code data directories.

    Returns only the most recent session per unique cwd path.
    """

    if claude_dir is None:
        claude_dir = os.path.expanduser("~/.claude")

    claude_path = Path(claude_dir)
    projects_dir = claude_path / "projects"

    all_sessions = []

    if not projects_dir.exists():
        return []

    # Iterate through all project directories
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue

        project_name = project_dir.name

        # Find all session .jsonl files (skip agent- subagent files)
        for session_file in project_dir.glob("*.jsonl"):
            if session_file.stem.startswith("agent-"):
                continue
            try:
                session = _load_session_file(session_file, project_name)
                if not session:
                    continue
                # Skip critic hook sessions
                if session.first_message.startswith("Evaluate if this bash"):
                    continue
                # Skip empty/useless sessions
                if not session.cwd and not session.first_message:
                    continue
                # Skip very short sessions (likely just started and abandoned)
                if session.context_size < 500:
                    continue
                session.category = categorize_session(session)
                all_sessions.append(session)
            except Exception:
                # Skip malformed session files
                continue

    # Keep only the session with the most content per path (largest file = most messages)
    by_path: dict[str, Session] = {}
    for s in all_sessions:
        path_key = s.cwd or s.project_path
        if path_key not in by_path or s.context_size > by_path[path_key].context_size:
            by_path[path_key] = s

    return list(by_path.values())


def _load_session_file(filepath: Path, project_name: str) -> Optional[Session]:
    """Load a single session from a .jsonl file."""

    stat = filepath.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime)
    context_size = stat.st_size

    # Parse session UUID from filename
    uuid = filepath.stem

    # Read file to extract metadata
    slug = ""
    cwd = ""
    git_branch = ""
    model = ""
    summary = ""
    first_message = ""
    last_texts = []  # Collect recent text for summarization

    with open(filepath, "r") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Extract summary
            if entry.get("type") == "summary":
                summary = entry.get("summary", "")
                continue

            # Extract session metadata from user/assistant messages
            if entry.get("sessionId"):
                if not slug:
                    slug = entry.get("slug", "")
                if not cwd:
                    cwd = entry.get("cwd", "")
                if not git_branch:
                    git_branch = entry.get("gitBranch", "")

            # Get first user message as preview
            if entry.get("type") == "user" and not first_message:
                msg = entry.get("message", {})
                content = msg.get("content", [])
                # Handle string content directly
                if isinstance(content, str):
                    first_message = content[:100]
                else:
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            first_message = item.get("text", "")[:100]
                            break
                        elif isinstance(item, str):
                            first_message = item[:100]
                            break

            # Get model from assistant messages
            if entry.get("type") == "assistant" and not model:
                msg = entry.get("message", {})
                model = msg.get("model", "")

            # Collect text from messages for last_summary
            if entry.get("type") in ("user", "assistant"):
                msg = entry.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, str):
                    last_texts.append(content)
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            last_texts.append(item.get("text", ""))
                        elif isinstance(item, str):
                            last_texts.append(item)

    if not uuid:
        return None

    # Build start context (~500 tokens / 2000 chars from beginning)
    context_start = ""
    for text in last_texts:
        if len(context_start) + len(text) > 2000:
            break
        context_start += text + "\n"

    # Build end context (~500 tokens / 2000 chars from end)
    context_end = ""
    for text in reversed(last_texts):
        if len(context_end) + len(text) > 2000:
            break
        context_end = text + "\n" + context_end

    return Session(
        uuid=uuid,
        slug=slug or uuid[:8],
        cwd=cwd,
        project_path=project_name,
        mtime=mtime,
        context_size=context_size,
        summary=summary,
        git_branch=git_branch,
        model=model,
        first_message=first_message,
        context_start=context_start.strip(),
        context_end=context_end.strip(),
    )
