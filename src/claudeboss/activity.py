"""Activity timeline reconstruction for Claude Code sessions.

Analyzes history.jsonl and debug logs to reconstruct when a session was worked on,
providing a detailed timeline of activity periods.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .session import Session


@dataclass
class ActivityPeriod:
    """A period of activity within a session."""
    start: datetime
    end: datetime
    message_count: int
    first_message: str

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def duration_display(self) -> str:
        """Human-readable duration."""
        secs = int(self.duration.total_seconds())
        if secs < 60:
            return f"{secs}s"
        mins = secs // 60
        if mins < 60:
            return f"{mins}m"
        hours = mins // 60
        mins = mins % 60
        if hours < 24:
            return f"{hours}h {mins}m" if mins else f"{hours}h"
        days = hours // 24
        hours = hours % 24
        return f"{days}d {hours}h" if hours else f"{days}d"

    @property
    def time_display(self) -> str:
        """Display format for the period."""
        return self.start.strftime("%b %d %H:%M")


@dataclass
class ActivityTimeline:
    """Complete activity timeline for a session."""
    session_id: str
    periods: list[ActivityPeriod]
    total_messages: int
    first_activity: Optional[datetime]
    last_activity: Optional[datetime]

    @property
    def total_duration(self) -> timedelta:
        """Sum of all activity period durations."""
        return sum((p.duration for p in self.periods), timedelta())

    @property
    def span(self) -> Optional[timedelta]:
        """Time between first and last activity."""
        if self.first_activity and self.last_activity:
            return self.last_activity - self.first_activity
        return None

    @property
    def active_days(self) -> int:
        """Number of unique days with activity."""
        days = set()
        for p in self.periods:
            days.add(p.start.date())
            days.add(p.end.date())
        return len(days)


def reconstruct_activity(session: Session, claude_dir: Optional[str] = None) -> ActivityTimeline:
    """Reconstruct the activity timeline for a session.

    Analyzes:
    1. history.jsonl for messages matching the session
    2. Debug log files for session timing
    3. Session file modification times

    Groups messages into activity periods (gaps > 30min = new period).
    """
    if claude_dir is None:
        claude_dir = os.path.expanduser("~/.claude")

    claude_path = Path(claude_dir)

    # Collect all timestamps for this session
    timestamps: list[tuple[datetime, str]] = []

    # 1. Search history.jsonl for matching project/sessionId
    history_file = claude_path / "history.jsonl"
    if history_file.exists():
        timestamps.extend(_search_history(history_file, session))

    # 2. Check debug logs for this session
    debug_dir = claude_path / "debug"
    if debug_dir.exists():
        timestamps.extend(_search_debug_logs(debug_dir, session))

    # 3. Check session file mtime as fallback
    session_file = _find_session_file(claude_path, session)
    if session_file:
        mtime = datetime.fromtimestamp(session_file.stat().st_mtime)
        if not timestamps or mtime > max(t[0] for t in timestamps):
            timestamps.append((mtime, "[session file modified]"))

    if not timestamps:
        return ActivityTimeline(
            session_id=session.uuid,
            periods=[],
            total_messages=0,
            first_activity=None,
            last_activity=None,
        )

    # Sort by timestamp
    timestamps.sort(key=lambda x: x[0])

    # Group into activity periods (gaps > 30 min = new period)
    periods = _group_into_periods(timestamps, gap_threshold=timedelta(minutes=30))

    return ActivityTimeline(
        session_id=session.uuid,
        periods=periods,
        total_messages=len(timestamps),
        first_activity=timestamps[0][0] if timestamps else None,
        last_activity=timestamps[-1][0] if timestamps else None,
    )


def _search_history(history_file: Path, session: Session) -> list[tuple[datetime, str]]:
    """Search history.jsonl for entries matching this session."""
    results = []

    # Build search criteria
    cwd = session.cwd or ("/" + session.project_path.replace("-", "/").lstrip("/"))
    session_id = session.uuid

    try:
        with open(history_file, 'r') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Match by sessionId if present
                if entry.get('sessionId') == session_id:
                    ts = entry.get('timestamp')
                    if ts:
                        dt = datetime.fromtimestamp(ts / 1000)  # ms to seconds
                        msg = entry.get('display', '')[:60]
                        results.append((dt, msg))
                    continue

                # Match by project path
                project = entry.get('project', '')
                if project and (project == cwd or project.rstrip('/') == cwd.rstrip('/')):
                    ts = entry.get('timestamp')
                    if ts:
                        dt = datetime.fromtimestamp(ts / 1000)
                        msg = entry.get('display', '')[:60]
                        results.append((dt, msg))
    except (IOError, OSError):
        pass

    return results


def _search_debug_logs(debug_dir: Path, session: Session) -> list[tuple[datetime, str]]:
    """Search debug logs for entries mentioning this session."""
    results = []
    session_id = session.uuid

    # Debug files are named with UUIDs
    debug_file = debug_dir / f"{session_id}.txt"
    if debug_file.exists():
        try:
            mtime = datetime.fromtimestamp(debug_file.stat().st_mtime)
            ctime = datetime.fromtimestamp(debug_file.stat().st_ctime)
            results.append((ctime, "[debug log created]"))
            if mtime != ctime:
                results.append((mtime, "[debug log modified]"))
        except (IOError, OSError):
            pass

    return results


def _find_session_file(claude_path: Path, session: Session) -> Optional[Path]:
    """Find the session's jsonl file."""
    cwd = session.cwd or ("/" + session.project_path.replace("-", "/"))
    project_path = cwd.replace("/.", "/-").replace("/", "-").lstrip("-")

    projects_dir = claude_path / "projects"

    for prefix in ["", "-"]:
        proj_dir = projects_dir / (prefix + project_path)
        if proj_dir.exists():
            session_file = proj_dir / f"{session.uuid}.jsonl"
            if session_file.exists():
                return session_file
    return None


def _group_into_periods(
    timestamps: list[tuple[datetime, str]],
    gap_threshold: timedelta
) -> list[ActivityPeriod]:
    """Group timestamps into activity periods based on gap threshold."""
    if not timestamps:
        return []

    periods = []
    period_start = timestamps[0][0]
    period_end = timestamps[0][0]
    period_messages = [timestamps[0][1]]

    for ts, msg in timestamps[1:]:
        gap = ts - period_end

        if gap > gap_threshold:
            # New period
            periods.append(ActivityPeriod(
                start=period_start,
                end=period_end,
                message_count=len(period_messages),
                first_message=period_messages[0] if period_messages else "",
            ))
            period_start = ts
            period_end = ts
            period_messages = [msg]
        else:
            # Continue current period
            period_end = ts
            period_messages.append(msg)

    # Final period
    periods.append(ActivityPeriod(
        start=period_start,
        end=period_end,
        message_count=len(period_messages),
        first_message=period_messages[0] if period_messages else "",
    ))

    return periods


def format_timeline_for_display(timeline: ActivityTimeline, max_width: int = 40) -> list[str]:
    """Format activity timeline for terminal display.

    Returns a list of lines suitable for rendering in the detail view.
    """
    lines = []

    if not timeline.periods:
        lines.append("No activity data found")
        return lines

    # Header with summary stats
    total_dur = timeline.total_duration
    hours = int(total_dur.total_seconds() // 3600)
    mins = int((total_dur.total_seconds() % 3600) // 60)

    if hours > 0:
        dur_str = f"{hours}h {mins}m" if mins else f"{hours}h"
    else:
        dur_str = f"{mins}m"

    lines.append(f"ACTIVITY: {dur_str} active over {timeline.active_days} day(s)")
    lines.append(f"Messages: {timeline.total_messages}")
    lines.append("")

    # Timeline visualization
    lines.append("Timeline:")

    for i, period in enumerate(timeline.periods):
        # Date line
        date_str = period.start.strftime("%b %d")
        time_str = period.start.strftime("%H:%M")
        dur_str = period.duration_display

        # Use Unicode box drawing for timeline
        if i == 0 and i == len(timeline.periods) - 1:
            connector = "●"  # Single period
        elif i == 0:
            connector = "┌"  # First of multiple
        elif i == len(timeline.periods) - 1:
            connector = "└"  # Last
        else:
            connector = "├"  # Middle

        # Main period line
        period_line = f" {connector}─ {date_str} {time_str}"
        if period.message_count > 1:
            period_line += f" ({period.message_count} msgs, {dur_str})"
        else:
            period_line += f" ({dur_str})"

        lines.append(period_line[:max_width])

        # Show first message preview (indented)
        if period.first_message and period.first_message not in ("[debug log", "[session"):
            preview = period.first_message[:max_width - 6]
            if len(period.first_message) > max_width - 6:
                preview += "..."
            # Indent continuation
            if i < len(timeline.periods) - 1:
                lines.append(f" │  └ \"{preview}\"")
            else:
                lines.append(f"    └ \"{preview}\"")

    # Show span if multiple days
    if timeline.span and timeline.span.days > 0:
        lines.append("")
        lines.append(f"Span: {timeline.span.days + 1} days from first to last")

    return lines
