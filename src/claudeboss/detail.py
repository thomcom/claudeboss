"""Detail view for claudeboss - session deep dive with file tree and temporal log."""

import curses
import hashlib
import json
import os
import random
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .session import Session
from .activity import reconstruct_activity, format_timeline_for_display

# Box drawing characters (matching ui.py)
BOX_H = "─"
BOX_V = "│"
BOX_TL = "╭"
BOX_TR = "╮"
BOX_BL = "╰"
BOX_BR = "╯"
BOX_L = "├"
BOX_R = "┤"
BOX_T = "┬"
BOX_B = "┴"

# Tree drawing characters
TREE_BRANCH = "├── "
TREE_LAST = "└── "
TREE_PIPE = "│   "
TREE_SPACE = "    "

# Cache for temporal logs
LOG_CACHE_DIR = Path.home() / ".cache" / "claudeboss"
LOG_CACHE_FILE = LOG_CACHE_DIR / "temporal_log_cache.json"

# Responsive breakpoints
WIDE_MODE_MIN_WIDTH = 80


@dataclass
class DetailState:
    """State for the detail view."""
    session: Session
    file_tree: list[str] = field(default_factory=list)
    temporal_log: list[str] = field(default_factory=list)
    activity_timeline: list[str] = field(default_factory=list)
    scroll_offset: int = 0
    log_generating: bool = False
    log_gen_frame: int = 0  # For animation

    # Metadata extracted from session
    prompt_count: int = 0
    estimated_tokens: int = 0
    estimated_cost: float = 0.0

    # Input mode state
    input_mode: str = ""  # "", "fork"
    input_prompt: str = ""
    input_value: str = ""
    input_cursor: int = 0


class SessionDetailView:
    """Full-screen detail view for a session."""

    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.state: Optional[DetailState] = None
        self._init_colors()

    def _init_colors(self):
        """Initialize color pairs (assumes already started by main app)."""
        # Colors should already be initialized by SessionListView
        pass

    def _safe_addstr(self, y: int, x: int, text: str, attr: int = 0, max_width: int = -1):
        """Safely add string, clipping to bounds."""
        height, width = self.stdscr.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width:
            return
        avail = width - x
        if max_width > 0:
            avail = min(avail, max_width)
        if avail <= 0:
            return
        text = text[:avail]
        if y == height - 1 and x + len(text) >= width:
            text = text[:width - x - 1]
        if not text:
            return
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def set_session(self, session: Session):
        """Set the session to display and load data."""
        self.state = DetailState(session=session)
        self._load_metadata()
        self._load_file_tree()
        self._load_activity()
        self._load_temporal_log()

    def _load_metadata(self):
        """Extract metadata from session file."""
        if not self.state:
            return

        session = self.state.session
        session_path = self._get_session_file_path()

        if not session_path or not session_path.exists():
            return

        prompt_count = 0
        try:
            with open(session_path, 'r') as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get('type') == 'user':
                            prompt_count += 1
                    except json.JSONDecodeError:
                        continue
        except (IOError, OSError):
            pass

        self.state.prompt_count = prompt_count
        # Rough estimates: ~4 chars per token, cost varies by model
        self.state.estimated_tokens = session.context_size // 4
        # Assume mixed input/output at ~$3/M input, $15/M output average
        self.state.estimated_cost = (self.state.estimated_tokens / 1_000_000) * 9

    def _get_session_file_path(self) -> Optional[Path]:
        """Get the path to the session's jsonl file."""
        if not self.state:
            return None
        session = self.state.session
        cwd = session.cwd or ("/" + session.project_path.replace("-", "/"))

        # Convert to project path format
        project_path = cwd.replace("/.", "/-").replace("/", "-").lstrip("-")

        claude_dir = Path.home() / ".claude" / "projects"

        # Try variations
        for prefix in ["", "-"]:
            proj_dir = claude_dir / (prefix + project_path)
            if proj_dir.exists():
                session_file = proj_dir / f"{session.uuid}.jsonl"
                if session_file.exists():
                    return session_file
        return None

    def _load_file_tree(self):
        """Load directory tree for the session's working directory."""
        if not self.state:
            return

        session = self.state.session
        cwd = session.cwd or ("/" + session.project_path.replace("-", "/"))

        if not os.path.isdir(cwd):
            self.state.file_tree = [f"[Directory not found: {cwd}]"]
            return

        tree_lines = []
        try:
            tree_lines = self._build_tree(Path(cwd), max_depth=3)
        except (OSError, PermissionError):
            tree_lines = [f"[Cannot read: {cwd}]"]

        self.state.file_tree = tree_lines if tree_lines else ["[Empty directory]"]

    def _build_tree(self, root: Path, max_depth: int = 3) -> list[str]:
        """Build a tree-style directory listing."""
        lines = [f"📁 {root.name}/"]

        def walk(path: Path, prefix: str, depth: int):
            if depth > max_depth:
                return

            try:
                entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
            except (OSError, PermissionError):
                return

            # Filter out hidden files and common noise
            entries = [e for e in entries if not e.name.startswith('.')
                      and e.name not in ('__pycache__', 'node_modules', '.git', 'venv', '.venv')]

            # Limit entries per level
            max_entries = 15
            truncated = len(entries) > max_entries
            entries = entries[:max_entries]

            for i, entry in enumerate(entries):
                is_last = (i == len(entries) - 1) and not truncated

                if entry.is_dir():
                    connector = TREE_LAST if is_last else TREE_BRANCH
                    lines.append(f"{prefix}{connector}📁 {entry.name}/")
                    new_prefix = prefix + (TREE_SPACE if is_last else TREE_PIPE)
                    walk(entry, new_prefix, depth + 1)
                else:
                    connector = TREE_LAST if is_last else TREE_BRANCH
                    lines.append(f"{prefix}{connector}{entry.name}")

            if truncated:
                lines.append(f"{prefix}{TREE_LAST}... ({len(entries)} more)")

        walk(root, "", 1)
        return lines

    def _load_activity(self):
        """Load activity timeline from history and debug logs."""
        if not self.state:
            return

        session = self.state.session
        try:
            timeline = reconstruct_activity(session)
            _, width = self.stdscr.getmaxyx()
            col_width = max(30, (width - 7) // 2)
            self.state.activity_timeline = format_timeline_for_display(timeline, max_width=col_width)
        except Exception:
            self.state.activity_timeline = ["[Activity data unavailable]"]

    def _load_temporal_log(self):
        """Load or generate temporal log summary."""
        if not self.state:
            return

        session = self.state.session

        # Check cache first
        cache = self._load_log_cache()
        cache_key = f"{session.uuid}:{self._stable_hash(session.context_start + session.context_end)}"

        if cache_key in cache:
            self.state.temporal_log = cache[cache_key]
            return

        # Generate via Haiku in background thread
        self.state.log_generating = True
        self.state.temporal_log = []  # Empty, will show animation

        thread = threading.Thread(
            target=self._generate_temporal_log_background,
            args=(cache_key,),
            daemon=True
        )
        thread.start()

    def _stable_hash(self, s: str) -> str:
        """Deterministic hash for cache keys."""
        return hashlib.md5(s.encode()).hexdigest()[:16]

    def _load_log_cache(self) -> dict:
        """Load temporal log cache."""
        LOG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_CACHE_FILE.exists():
            try:
                with open(LOG_CACHE_FILE) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {}

    def _save_log_cache(self, cache: dict):
        """Save temporal log cache."""
        try:
            with open(LOG_CACHE_FILE, 'w') as f:
                json.dump(cache, f)
        except IOError:
            pass

    def _generate_temporal_log_background(self, cache_key: str):
        """Generate temporal log via Haiku CLI in background thread."""
        if not self.state:
            return

        session = self.state.session
        context = f"START:\n{session.context_start[:3000]}\n\nEND:\n{session.context_end[:3000]}"

        prompt = f"""Analyze this Claude Code session transcript and produce a temporal log.
Format as exactly 5 bracketed sections, each 1-2 sentences max:

[Initial] What the user originally asked for
[Proposal] How Claude planned to approach it
[Work] What was actually built/modified
[Challenges] Any difficulties or pivots encountered
[Current] Final state and what's working now

Be specific - mention actual file names, features, or concepts from the transcript.
Plain text only, no markdown.

SESSION TRANSCRIPT:
{context}

TEMPORAL LOG:"""

        try:
            result = subprocess.run(
                ["claude", "-p", "--model", "haiku", "--tools", ""],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=60,
                cwd="/tmp"
            )

            if result.returncode == 0 and result.stdout.strip():
                log_text = result.stdout.strip()
                # Parse into lines, clean up
                lines = []
                for line in log_text.split('\n'):
                    line = line.strip().replace('**', '').replace('*', '')
                    if line and (line.startswith('[') or lines):
                        lines.append(line)

                if lines:
                    self.state.temporal_log = lines
                    self.state.log_generating = False

                    # Cache it
                    cache = self._load_log_cache()
                    cache[cache_key] = lines
                    self._save_log_cache(cache)
                    return

        except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
            pass

        self.state.temporal_log = ["[Unable to generate log]"]
        self.state.log_generating = False

    def render(self):
        """Render the detail view."""
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()

        if not self.state:
            self._safe_addstr(1, 2, "No session selected")
            self.stdscr.refresh()
            return

        # Determine layout mode
        wide_mode = width >= WIDE_MODE_MIN_WIDTH

        self._render_border(height, width)
        self._render_title(height, width)
        self._render_metadata(height, width)

        if wide_mode:
            self._render_two_column(height, width)
        else:
            self._render_single_column(height, width)

        # Render input overlay if in input mode
        if self.state.input_mode:
            self._render_input_dialog(height, width)
        else:
            self._render_help(height, width)

        self.stdscr.refresh()

    def _render_border(self, height: int, width: int):
        """Draw border matching list view style."""
        self.stdscr.attron(curses.color_pair(1))

        # Top border
        top = BOX_TL + BOX_H * max(0, width - 2) + BOX_TR
        self._safe_addstr(0, 0, top)

        # Side borders
        for y in range(1, height - 1):
            self._safe_addstr(y, 0, BOX_V)
            if width > 1:
                self._safe_addstr(y, width - 1, BOX_V)

        # Bottom border
        if height > 1:
            bottom = BOX_BL + BOX_H * max(0, width - 3) + BOX_BR
            self._safe_addstr(height - 1, 0, bottom)

        # Separator after title (row 2)
        if height > 4:
            sep = BOX_L + BOX_H * max(0, width - 2) + BOX_R
            self._safe_addstr(2, 0, sep)

        # Separator after metadata (row 5 for wide, 6 for narrow)
        meta_sep_row = 5 if width >= WIDE_MODE_MIN_WIDTH else 6
        if height > meta_sep_row + 2:
            sep = BOX_L + BOX_H * max(0, width - 2) + BOX_R
            self._safe_addstr(meta_sep_row, 0, sep)

        # Separator before help
        if height > 6:
            sep = BOX_L + BOX_H * max(0, width - 2) + BOX_R
            self._safe_addstr(height - 3, 0, sep)

        self.stdscr.attroff(curses.color_pair(1))

    def _render_title(self, height: int, width: int):
        """Render title bar with session summary as title."""
        if height < 3 or not self.state:
            return

        inner_width = width - 4
        if inner_width < 1:
            return

        # Use last_summary as the title, fallback to dir_name
        session = self.state.session
        title = session.last_summary or session.dir_name or session.slug

        # Truncate if needed
        if len(title) > inner_width - 4:
            title = title[:inner_width - 7] + "..."

        # Center the title
        pad = max(2, (width - len(title) - 2) // 2)
        self._safe_addstr(1, pad, f" {title} ", curses.color_pair(1) | curses.A_BOLD)

    def _render_metadata(self, height: int, width: int):
        """Render metadata section."""
        if height < 6 or not self.state:
            return

        inner_width = width - 4
        session = self.state.session
        state = self.state

        # Build metadata strings
        path_str = session.short_path
        when_str = session.mtime_display
        size_str = session.context_display
        prompts_str = f"{state.prompt_count} prompts"
        tokens_str = f"~{state.estimated_tokens // 1000}K tok"
        cost_str = f"~${state.estimated_cost:.2f}"
        model_str = session.model.split('-')[-1] if session.model else "unknown"

        if width >= WIDE_MODE_MIN_WIDTH:
            # Wide: two rows of metadata
            row1 = f"PATH: {path_str}"
            row2 = f"LAST: {when_str}  SIZE: {size_str}  {prompts_str}  {tokens_str}  COST: {cost_str}"

            self._safe_addstr(3, 2, row1, curses.color_pair(2), inner_width)
            self._safe_addstr(4, 2, row2, curses.A_DIM, inner_width)
        else:
            # Narrow: three compact rows
            self._safe_addstr(3, 2, f"PATH: {path_str}", curses.color_pair(2), inner_width)
            self._safe_addstr(4, 2, f"LAST: {when_str}  SIZE: {size_str}", curses.A_DIM, inner_width)
            self._safe_addstr(5, 2, f"{prompts_str}  {cost_str}", curses.A_DIM, inner_width)

    def _render_two_column(self, height: int, width: int):
        """Render two-column layout (file tree left, temporal log right)."""
        if not self.state:
            return

        inner_width = width - 4
        col_width = (inner_width - 3) // 2  # -3 for divider and spacing

        start_row = 6
        end_row = height - 4
        visible_rows = end_row - start_row

        if visible_rows < 1:
            return

        # Draw vertical divider
        divider_x = 2 + col_width + 1
        self.stdscr.attron(curses.color_pair(1))
        for y in range(start_row, end_row + 1):
            self._safe_addstr(y, divider_x, BOX_V)
        # Connect divider to horizontal separators
        self._safe_addstr(5, divider_x, BOX_T)
        self._safe_addstr(height - 3, divider_x, BOX_B)
        self.stdscr.attroff(curses.color_pair(1))

        # Left column: file tree
        tree = self.state.file_tree
        for i in range(min(visible_rows, len(tree) - self.state.scroll_offset)):
            idx = self.state.scroll_offset + i
            if idx < len(tree):
                line = tree[idx][:col_width]
                color = curses.color_pair(4) if "📁" in line else curses.A_DIM
                self._safe_addstr(start_row + i, 2, line, color, col_width)

        # Right column: activity timeline + temporal log
        log_x = divider_x + 2

        # Activity timeline section (compact, at top)
        activity = self.state.activity_timeline
        activity_rows = min(len(activity), visible_rows // 3)  # Use up to 1/3 of space

        for i in range(activity_rows):
            line = activity[i][:col_width]
            # Highlight ACTIVITY header
            if line.startswith("ACTIVITY:"):
                self._safe_addstr(start_row + i, log_x, line, curses.color_pair(5) | curses.A_BOLD, col_width)
            elif line.startswith("Timeline:") or line.startswith("Messages:") or line.startswith("Span:"):
                self._safe_addstr(start_row + i, log_x, line, curses.color_pair(2), col_width)
            elif "─" in line or "●" in line or "┌" in line or "├" in line or "└" in line:
                self._safe_addstr(start_row + i, log_x, line, curses.color_pair(4), col_width)
            else:
                self._safe_addstr(start_row + i, log_x, line, curses.A_DIM, col_width)

        # Temporal log section (below activity)
        log = self._wrap_lines(self.state.temporal_log, col_width)
        log_start = start_row + activity_rows + 1
        log_rows = visible_rows - activity_rows - 2

        # Section header
        self._safe_addstr(log_start, log_x, "TEMPORAL LOG", curses.color_pair(1) | curses.A_BOLD, col_width)
        log_start += 2

        # Show generating animation if in progress
        if self.state.log_generating:
            self._render_generating_animation(log_start, log_x, col_width)
        else:
            for i in range(min(log_rows, len(log) - self.state.scroll_offset)):
                idx = self.state.scroll_offset + i
                if idx < len(log):
                    line = log[idx][:col_width]
                    # Highlight section headers
                    if line.startswith('['):
                        bracket_end = line.find(']')
                        if bracket_end > 0:
                            self._safe_addstr(log_start + i, log_x, line[:bracket_end + 1],
                                             curses.color_pair(3) | curses.A_BOLD, col_width)
                            self._safe_addstr(log_start + i, log_x + bracket_end + 1,
                                             line[bracket_end + 1:], curses.A_DIM, col_width - bracket_end - 1)
                        else:
                            self._safe_addstr(log_start + i, log_x, line, curses.A_DIM, col_width)
                    else:
                        self._safe_addstr(log_start + i, log_x, line, curses.A_DIM, col_width)

    def _wrap_lines(self, lines: list[str], width: int) -> list[str]:
        """Word-wrap lines to fit within width."""
        wrapped = []
        for line in lines:
            if len(line) <= width:
                wrapped.append(line)
            else:
                # Word wrap
                words = line.split()
                current = ""
                for word in words:
                    if not current:
                        current = word
                    elif len(current) + 1 + len(word) <= width:
                        current += " " + word
                    else:
                        wrapped.append(current)
                        current = "  " + word  # Indent continuation
                if current:
                    wrapped.append(current)
        return wrapped

    def _render_generating_animation(self, row: int, x: int, max_width: int):
        """Render animated 'Generating...' text with brightness wave."""
        if not self.state:
            return

        text = "Generating..."
        # Advance frame randomly for staggered effect
        if random.random() < 0.3:
            self.state.log_gen_frame = (self.state.log_gen_frame + 1) % 22

        phase = self.state.log_gen_frame
        color = curses.color_pair(3)  # Yellow

        for ci, ch in enumerate(text):
            # Phase 0-10: bold wave; 11-21: dim wave
            if phase <= 10:
                attr = curses.A_BOLD if ci < phase else curses.A_DIM
            else:
                attr = curses.A_DIM if ci < (phase - 11) else curses.A_BOLD
            try:
                self.stdscr.addch(row, x + ci, ch, color | attr)
            except curses.error:
                pass

    def _render_single_column(self, height: int, width: int):
        """Render single-column scrollable layout."""
        if not self.state:
            return

        inner_width = width - 4
        start_row = 7
        end_row = height - 4
        visible_rows = end_row - start_row

        if visible_rows < 1:
            return

        # Combine file tree, activity, and temporal log into single scrollable content
        combined = []
        combined.extend(self.state.file_tree)
        combined.append("")
        combined.append("─── ACTIVITY ───")
        combined.append("")
        combined.extend(self.state.activity_timeline)
        combined.append("")
        combined.append("─── TEMPORAL LOG ───")
        combined.append("")

        # Track where temporal log section starts for animation
        log_section_start = len(combined)

        if self.state.log_generating:
            combined.append("")  # Placeholder for animation
        else:
            # Wrap temporal log lines to fit width
            combined.extend(self._wrap_lines(self.state.temporal_log, inner_width))

        for i in range(min(visible_rows, len(combined) - self.state.scroll_offset)):
            idx = self.state.scroll_offset + i
            if idx < len(combined):
                line = combined[idx][:inner_width]

                # Show generating animation at the right spot
                if self.state.log_generating and idx == log_section_start:
                    self._render_generating_animation(start_row + i, 2, inner_width)
                    continue

                # Color based on content
                if "📁" in line:
                    color = curses.color_pair(4)
                elif line.startswith("───"):
                    color = curses.color_pair(1) | curses.A_BOLD
                elif line.startswith("ACTIVITY:"):
                    color = curses.color_pair(5) | curses.A_BOLD
                elif line.startswith("Timeline:") or line.startswith("Messages:") or line.startswith("Span:"):
                    color = curses.color_pair(2)
                elif "─" in line and ("●" in line or "┌" in line or "├" in line or "└" in line):
                    color = curses.color_pair(4)
                elif line.startswith('['):
                    bracket_end = line.find(']')
                    if bracket_end > 0:
                        self._safe_addstr(start_row + i, 2, line[:bracket_end + 1],
                                         curses.color_pair(3) | curses.A_BOLD, inner_width)
                        self._safe_addstr(start_row + i, 2 + bracket_end + 1,
                                         line[bracket_end + 1:], curses.A_DIM, inner_width - bracket_end - 1)
                        continue
                    color = curses.A_DIM
                else:
                    color = curses.A_DIM

                self._safe_addstr(start_row + i, 2, line, color, inner_width)

    def _render_help(self, height: int, width: int):
        """Render help bar."""
        if height < 4:
            return

        inner_width = width - 4

        if inner_width > 50:
            help_text = "h:back  l:resume  f:fork  j/k:scroll"
        elif inner_width > 40:
            help_text = "h:back l:resume f:fork jk:nav"
        else:
            help_text = "h:back l:resume f:fork"

        self._safe_addstr(height - 2, 2, help_text, curses.color_pair(7) | curses.A_DIM, inner_width)

        # Scroll indicator
        if self.state:
            max_scroll = self._get_max_scroll()
            if max_scroll > 0:
                pos = f"[{self.state.scroll_offset + 1}/{max_scroll + 1}]"
                pos_x = width - len(pos) - 2
                if pos_x > 2:
                    self._safe_addstr(height - 2, pos_x, pos, curses.color_pair(2))

    def _render_input_dialog(self, height: int, width: int):
        """Render an input dialog overlay."""
        if not self.state:
            return

        # Dialog dimensions
        dialog_width = min(60, width - 8)
        dialog_height = 5
        dialog_x = (width - dialog_width) // 2
        dialog_y = (height - dialog_height) // 2

        # Draw dialog box
        self.stdscr.attron(curses.color_pair(1))

        # Top border
        self._safe_addstr(dialog_y, dialog_x, BOX_TL + BOX_H * (dialog_width - 2) + BOX_TR)

        # Sides and content area
        for i in range(1, dialog_height - 1):
            self._safe_addstr(dialog_y + i, dialog_x, BOX_V)
            self._safe_addstr(dialog_y + i, dialog_x + dialog_width - 1, BOX_V)
            # Clear content area
            self._safe_addstr(dialog_y + i, dialog_x + 1, " " * (dialog_width - 2))

        # Bottom border
        self._safe_addstr(dialog_y + dialog_height - 1, dialog_x,
                         BOX_BL + BOX_H * (dialog_width - 2) + BOX_BR)

        self.stdscr.attroff(curses.color_pair(1))

        # Prompt text
        self._safe_addstr(dialog_y + 1, dialog_x + 2, self.state.input_prompt,
                         curses.color_pair(3) | curses.A_BOLD, dialog_width - 4)

        # Input field
        input_x = dialog_x + 2
        input_y = dialog_y + 2
        input_width = dialog_width - 4

        # Draw input value with cursor
        value = self.state.input_value
        if len(value) > input_width - 1:
            # Scroll to show cursor
            start = max(0, self.state.input_cursor - input_width + 2)
            value = value[start:start + input_width - 1]

        self._safe_addstr(input_y, input_x, value, curses.A_NORMAL, input_width)

        # Show cursor
        cursor_pos = min(self.state.input_cursor, input_width - 1)
        try:
            curses.curs_set(1)  # Show cursor
            self.stdscr.move(input_y, input_x + cursor_pos)
        except curses.error:
            pass

        # Help text
        self._safe_addstr(dialog_y + 3, dialog_x + 2, "Enter:confirm  Esc:cancel",
                         curses.A_DIM, dialog_width - 4)

    def start_input(self, mode: str, prompt: str, default: str = ""):
        """Start input mode with the given prompt and default value."""
        if not self.state:
            return
        self.state.input_mode = mode
        self.state.input_prompt = prompt
        self.state.input_value = default
        self.state.input_cursor = len(default)

    def cancel_input(self):
        """Cancel input mode."""
        if not self.state:
            return
        self.state.input_mode = ""
        self.state.input_prompt = ""
        self.state.input_value = ""
        self.state.input_cursor = 0
        try:
            curses.curs_set(0)  # Hide cursor
        except curses.error:
            pass

    def _get_max_scroll(self) -> int:
        """Calculate maximum scroll offset."""
        if not self.state:
            return 0

        height, width = self.stdscr.getmaxyx()

        if width >= WIDE_MODE_MIN_WIDTH:
            # Two column: scroll based on longer of tree/log (activity is fixed at top)
            content_len = max(len(self.state.file_tree), len(self.state.temporal_log))
            visible = height - 10
        else:
            # Single column: combined content (file tree + activity + temporal log + separators)
            content_len = (len(self.state.file_tree) + len(self.state.activity_timeline) +
                          len(self.state.temporal_log) + 6)  # 6 for separators and spacing
            visible = height - 11

        return max(0, content_len - visible)

    def handle_key(self, key: int) -> Optional[str]:
        """Handle keyboard input. Returns action string or None.

        Navigation follows ncdu/vim conventions:
        - j/k/↑/↓: scroll
        - l/Enter/→: forward (resume session)
        - f: fork session to new directory
        - h/←/q/ESC: back to list
        """
        if not self.state:
            return None

        # Handle input mode separately
        if self.state.input_mode:
            return self._handle_input_key(key)

        # Vertical scrolling
        if key in (ord("j"), curses.KEY_DOWN):
            max_scroll = self._get_max_scroll()
            self.state.scroll_offset = min(self.state.scroll_offset + 1, max_scroll)
        elif key in (ord("k"), curses.KEY_UP):
            self.state.scroll_offset = max(0, self.state.scroll_offset - 1)
        elif key == curses.KEY_PPAGE or key == 21:  # Ctrl+U
            height, _ = self.stdscr.getmaxyx()
            page = max(1, height - 12)
            self.state.scroll_offset = max(0, self.state.scroll_offset - page)
        elif key == curses.KEY_NPAGE or key == 4:  # Ctrl+D
            height, _ = self.stdscr.getmaxyx()
            page = max(1, height - 12)
            max_scroll = self._get_max_scroll()
            self.state.scroll_offset = min(self.state.scroll_offset + page, max_scroll)
        elif key in (ord("g"), curses.KEY_HOME):
            self.state.scroll_offset = 0
        elif key in (ord("G"), curses.KEY_END):
            self.state.scroll_offset = self._get_max_scroll()

        # Forward: l/Enter/→ = resume session
        elif key in (ord("l"), ord("\n"), curses.KEY_ENTER, curses.KEY_RIGHT, 10, 13):
            return "resume"

        # Fork: f = fork session to new directory
        elif key == ord("f"):
            # Default to home directory
            default_dir = str(Path.home())
            self.start_input("fork", "Fork session into directory:", default_dir)

        # Back: h/←/q/ESC = back to list
        elif key in (ord("h"), curses.KEY_LEFT, ord("q"), 27):
            return "back"

        return None

    def _handle_input_key(self, key: int) -> Optional[str]:
        """Handle keyboard input while in input mode."""
        if not self.state or not self.state.input_mode:
            return None

        # Cancel on Escape
        if key == 27:
            self.cancel_input()
            return None

        # Submit on Enter
        if key in (ord("\n"), curses.KEY_ENTER, 10, 13):
            mode = self.state.input_mode
            value = self.state.input_value
            self.cancel_input()

            if mode == "fork" and value:
                # Expand ~ and resolve path
                expanded = os.path.expanduser(value)
                return f"fork:{expanded}"

            return None

        # Backspace
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if self.state.input_cursor > 0:
                self.state.input_value = (
                    self.state.input_value[:self.state.input_cursor - 1] +
                    self.state.input_value[self.state.input_cursor:]
                )
                self.state.input_cursor -= 1
            return None

        # Delete
        if key == curses.KEY_DC:
            if self.state.input_cursor < len(self.state.input_value):
                self.state.input_value = (
                    self.state.input_value[:self.state.input_cursor] +
                    self.state.input_value[self.state.input_cursor + 1:]
                )
            return None

        # Cursor movement
        if key == curses.KEY_LEFT:
            self.state.input_cursor = max(0, self.state.input_cursor - 1)
            return None
        if key == curses.KEY_RIGHT:
            self.state.input_cursor = min(len(self.state.input_value), self.state.input_cursor + 1)
            return None
        if key == curses.KEY_HOME or key == 1:  # Ctrl+A
            self.state.input_cursor = 0
            return None
        if key == curses.KEY_END or key == 5:  # Ctrl+E
            self.state.input_cursor = len(self.state.input_value)
            return None

        # Clear line: Ctrl+U
        if key == 21:
            self.state.input_value = ""
            self.state.input_cursor = 0
            return None

        # Regular character input
        if 32 <= key <= 126:
            ch = chr(key)
            self.state.input_value = (
                self.state.input_value[:self.state.input_cursor] +
                ch +
                self.state.input_value[self.state.input_cursor:]
            )
            self.state.input_cursor += 1

        return None
