"""Detect currently active Claude Code sessions."""

import os
import re
import subprocess
from pathlib import Path
from datetime import datetime, timedelta


def get_active_cwds() -> set[str]:
    """Get working directories of all running claude processes."""
    active_cwds = set()

    try:
        result = subprocess.run(
            ["pgrep", "-x", "claude"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0:
            return active_cwds

        for pid in result.stdout.strip().split('\n'):
            if not pid:
                continue
            cwd_path = f"/proc/{pid}/cwd"
            try:
                cwd = os.readlink(cwd_path)
                active_cwds.add(cwd)
            except (OSError, FileNotFoundError):
                continue

    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return active_cwds


def get_claude_window_titles() -> list[str]:
    """Get terminal window titles for active Claude sessions via X11.

    Returns titles like "✳ GPU Virtualization Testing".
    """
    titles = []
    try:
        # Get all windows with ✳ prefix (Claude's marker)
        result = subprocess.run(
            ["wmctrl", "-l"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0:
            return titles

        for line in result.stdout.strip().split('\n'):
            if '✳' in line:
                # Extract title after hostname (format: "0x... N hostname TITLE")
                parts = line.split(None, 3)
                if len(parts) >= 4:
                    titles.append(parts[3])
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return titles


def get_active_claude_sessions() -> list[dict]:
    """Get detailed info about active Claude sessions via X11 window inspection.

    Returns list of dicts with: title, claude_pid, cwd, terminal_pid
    """
    sessions = []

    try:
        # Get windows with ✳ marker
        result = subprocess.run(
            ["wmctrl", "-l", "-p"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0:
            return sessions

        for line in result.stdout.strip().split('\n'):
            if '✳' not in line:
                continue

            # Format: "0xWINID DESKTOP PID HOSTNAME TITLE..."
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue

            win_id, desktop, term_pid, hostname, title = parts

            # Find claude process in terminal's process tree
            try:
                pstree_result = subprocess.run(
                    ["pstree", "-p", term_pid],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                # Extract claude PID from pstree output like "claude(12345)"
                match = re.search(r'claude\((\d+)\)', pstree_result.stdout)
                if match:
                    claude_pid = match.group(1)
                    cwd = ""
                    try:
                        cwd = os.readlink(f"/proc/{claude_pid}/cwd")
                    except OSError:
                        pass

                    sessions.append({
                        'title': title,
                        'claude_pid': int(claude_pid),
                        'cwd': cwd,
                        'terminal_pid': int(term_pid),
                    })
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue

    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return sessions


def cwd_to_project_path(cwd: str) -> str:
    """Convert CWD to Claude's project path format.

    /home/devkit/foo/bar → home-devkit-foo-bar
    /home/devkit/.local/foo → home-devkit--local-foo (dot becomes extra dash)
    """
    # Replace / with -, but dotfiles get an extra dash
    # /home/.local → -home--local (leading dot in component = extra dash)
    result = cwd.replace("/.", "/-").replace("/", "-").lstrip("-")
    return result


def get_sessions_in_project(project_dir: Path) -> list[tuple[str, datetime, int]]:
    """Get all non-agent session UUIDs in a project directory.

    Returns list of (uuid, mtime, size) tuples.
    """
    sessions = []

    try:
        for f in project_dir.glob("*.jsonl"):
            # Skip agent files
            if f.stem.startswith("agent-"):
                continue
            stat = f.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime)
            sessions.append((f.stem, mtime, stat.st_size))
    except OSError:
        pass

    return sessions


def find_project_dir(projects_dir: Path, cwd: str) -> Path | None:
    """Find the project directory for a CWD, handling path encoding variations.

    Claude encodes paths inconsistently - sometimes _ becomes -, sometimes not.
    Also handles leading dash variations.
    """
    project_path = cwd_to_project_path(cwd)

    # Try exact match first
    candidates = [
        projects_dir / project_path,
        projects_dir / ("-" + project_path),
    ]

    # Also try with underscores replaced by hyphens (Claude does this sometimes)
    alt_path = project_path.replace("_", "-")
    if alt_path != project_path:
        candidates.append(projects_dir / alt_path)
        candidates.append(projects_dir / ("-" + alt_path))

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return None


def get_active_session_uuids(claude_dir: str | None = None) -> set[str]:
    """Get UUIDs of currently active sessions.

    Uses X11 window inspection to find active Claude sessions, then matches
    each to its session file by CWD. For CWDs with multiple sessions, picks
    the N largest where N = number of claude processes in that CWD.
    """
    if claude_dir is None:
        claude_dir = os.path.expanduser("~/.claude")

    projects_dir = Path(claude_dir) / "projects"
    if not projects_dir.exists():
        return set()

    active_uuids = set()

    # Get active sessions via X11 window inspection
    active_sessions = get_active_claude_sessions()

    # Count how many sessions per CWD
    cwd_counts: dict[str, int] = {}
    for sess in active_sessions:
        cwd = sess['cwd']
        cwd_counts[cwd] = cwd_counts.get(cwd, 0) + 1

    # Track how many we've picked per CWD
    cwd_picked: dict[str, int] = {}

    for cwd, count in cwd_counts.items():
        project_dir = find_project_dir(projects_dir, cwd)
        if not project_dir:
            continue

        sessions = get_sessions_in_project(project_dir)
        if sessions:
            # Sort by size (largest first) and pick top N
            sessions.sort(key=lambda x: x[2], reverse=True)
            for uuid, mtime, size in sessions[:count]:
                active_uuids.add(uuid)

    # Fallback: if X11 detection fails, use process-based detection
    if not active_uuids:
        active_cwds = get_active_cwds()
        for cwd in active_cwds:
            project_dir = find_project_dir(projects_dir, cwd)
            if not project_dir:
                continue
            sessions = get_sessions_in_project(project_dir)
            if sessions:
                sessions.sort(key=lambda x: x[2], reverse=True)
                active_uuids.add(sessions[0][0])

    return active_uuids


def refresh_active_status(sessions: list, active_uuids: set[str] | None = None) -> None:
    """Update is_active flag on session objects.

    Modifies sessions in place.
    """
    if active_uuids is None:
        active_uuids = get_active_session_uuids()

    for session in sessions:
        session.is_active = session.uuid in active_uuids
