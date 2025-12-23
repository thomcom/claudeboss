"""Ncurses UI components for claudeboss - BBS style."""

import curses
import random
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from .session import Session


class SortMode(Enum):
    MTIME = auto()
    CONTEXT_SIZE = auto()


class CategoryFilter(Enum):
    BOTH = auto()
    PERSONAL = auto()
    PROFESSIONAL = auto()


# Box drawing characters (rounded corners for modern aesthetic)
BOX_H = "─"
BOX_V = "│"
BOX_TL = "╭"
BOX_TR = "╮"
BOX_BL = "╰"
BOX_BR = "╯"
BOX_L = "├"
BOX_R = "┤"

# Minimum dimensions for rendering
MIN_WIDTH = 7
MIN_HEIGHT = 7


@dataclass
class ListState:
    """State for the session list view."""

    sessions: list[Session]
    filtered: list[Session]
    cursor: int = 0
    scroll_offset: int = 0
    sort_mode: SortMode = SortMode.MTIME
    category_filter: CategoryFilter = CategoryFilter.BOTH
    page_size: int = 20

    def apply_filter(self):
        """Apply current filter and sort to sessions."""
        if self.category_filter == CategoryFilter.BOTH:
            self.filtered = self.sessions[:]
        elif self.category_filter == CategoryFilter.PERSONAL:
            self.filtered = [s for s in self.sessions if s.category == "personal"]
        else:
            self.filtered = [s for s in self.sessions if s.category == "professional"]

        if self.sort_mode == SortMode.MTIME:
            self.filtered.sort(key=lambda s: s.mtime, reverse=True)
        else:
            self.filtered.sort(key=lambda s: s.context_size, reverse=True)

        if self.cursor >= len(self.filtered):
            self.cursor = max(0, len(self.filtered) - 1)

    def move_cursor(self, delta: int):
        """Move cursor by delta, clamping to bounds."""
        self.cursor = max(0, min(len(self.filtered) - 1, self.cursor + delta))
        self._adjust_scroll()

    def _adjust_scroll(self):
        """Adjust scroll offset to keep cursor visible."""
        if self.cursor < self.scroll_offset:
            self.scroll_offset = self.cursor
        elif self.cursor >= self.scroll_offset + self.page_size:
            self.scroll_offset = self.cursor - self.page_size + 1

    def toggle_sort(self):
        """Toggle between sort modes."""
        if self.sort_mode == SortMode.MTIME:
            self.sort_mode = SortMode.CONTEXT_SIZE
        else:
            self.sort_mode = SortMode.MTIME
        self.apply_filter()

    def cycle_category(self):
        """Cycle through category filters."""
        if self.category_filter == CategoryFilter.BOTH:
            self.category_filter = CategoryFilter.PERSONAL
        elif self.category_filter == CategoryFilter.PERSONAL:
            self.category_filter = CategoryFilter.PROFESSIONAL
        else:
            self.category_filter = CategoryFilter.BOTH
        self.apply_filter()

    @property
    def current_session(self) -> Optional[Session]:
        """Get currently selected session."""
        if 0 <= self.cursor < len(self.filtered):
            return self.filtered[self.cursor]
        return None


class SessionListView:
    """Ncurses view for session list - BBS style."""

    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.state: Optional[ListState] = None
        self.frame: int = 0
        self._thinking_phase: dict[str, int] = {}
        self._init_colors()

    def _init_colors(self):
        """Initialize color pairs."""
        curses.start_color()
        curses.use_default_colors()

        curses.init_pair(1, curses.COLOR_CYAN, -1)      # Title/borders
        curses.init_pair(2, curses.COLOR_GREEN, -1)     # Stats
        curses.init_pair(3, curses.COLOR_YELLOW, -1)    # Work sessions
        curses.init_pair(4, curses.COLOR_BLUE, -1)      # Personal sessions
        curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_BLUE)  # Status bar
        curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_CYAN)  # Selected row
        curses.init_pair(7, curses.COLOR_MAGENTA, -1)   # Keys
        curses.init_pair(8, curses.COLOR_GREEN, -1)     # Active sessions (bright green)

    def _safe_addstr(self, y: int, x: int, text: str, attr: int = 0, max_width: int = -1):
        """Safely add string, clipping to bounds and avoiding bottom-right corner."""
        height, width = self.stdscr.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width:
            return
        # Clip text to available width
        avail = width - x
        if max_width > 0:
            avail = min(avail, max_width)
        if avail <= 0:
            return
        text = text[:avail]
        # Avoid writing to bottom-right corner (causes scroll)
        if y == height - 1 and x + len(text) >= width:
            text = text[:width - x - 1]
        if not text:
            return
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass  # Ignore edge case errors

    def set_sessions(self, sessions: list[Session]):
        """Set the session list."""
        height, _ = self.stdscr.getmaxyx()
        self.state = ListState(
            sessions=sessions,
            filtered=[],
            page_size=max(1, height - 8),
        )
        self.state.apply_filter()

    def update_page_size(self):
        """Update page size based on current terminal dimensions."""
        height, _ = self.stdscr.getmaxyx()
        if self.state:
            self.state.page_size = max(1, height - 8)
            self.state._adjust_scroll()

    def render(self, summary_progress: tuple[int, int] = (0, 0)):
        """Render the full view."""
        self.frame += 1
        self._summary_progress = summary_progress
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()

        # Too small - just show frame
        if width < MIN_WIDTH or height < MIN_HEIGHT:
            self._render_minimal_frame(height, width)
            self.stdscr.refresh()
            return

        if not self.state:
            self._safe_addstr(0, 0, "Loading...")
            self.stdscr.refresh()
            return

        self._render_border(height, width)
        self._render_title(height, width)
        self._render_stats(height, width)
        self._render_sessions(height, width)
        self._render_help(height, width)
        self.stdscr.refresh()

    def _render_minimal_frame(self, height: int, width: int):
        """Render just the border at tiny sizes."""
        self.stdscr.attron(curses.color_pair(1))
        for y in range(height):
            for x in range(width):
                if y == 0:
                    if x == 0:
                        ch = BOX_TL
                    elif x == width - 1:
                        ch = BOX_TR
                    else:
                        ch = BOX_H
                elif y == height - 1:
                    if x == 0:
                        ch = BOX_BL
                    elif x == width - 1:
                        ch = BOX_BR
                    else:
                        ch = BOX_H
                elif x == 0 or x == width - 1:
                    ch = BOX_V
                else:
                    continue
                # Avoid bottom-right corner
                if y == height - 1 and x == width - 1:
                    continue
                try:
                    self.stdscr.addstr(y, x, ch)
                except curses.error:
                    pass
        self.stdscr.attroff(curses.color_pair(1))

    def _render_border(self, height: int, width: int):
        """Draw BBS-style border."""
        self.stdscr.attron(curses.color_pair(1))

        # Top border
        top = BOX_TL + BOX_H * max(0, width - 2) + BOX_TR
        self._safe_addstr(0, 0, top)

        # Side borders
        for y in range(1, height - 1):
            self._safe_addstr(y, 0, BOX_V)
            if width > 1:
                self._safe_addstr(y, width - 1, BOX_V)

        # Bottom border (careful with bottom-right)
        if height > 1:
            # Write bottom row excluding last char to avoid scroll
            bottom = BOX_BL + BOX_H * max(0, width - 3) + BOX_BR
            self._safe_addstr(height - 1, 0, bottom)

        # Separator after title (row 2) if enough height
        if height > 4:
            sep = BOX_L + BOX_H * max(0, width - 2) + BOX_R
            self._safe_addstr(2, 0, sep)

        # Separator before help (row height-3) if enough height
        if height > 6:
            sep = BOX_L + BOX_H * max(0, width - 2) + BOX_R
            self._safe_addstr(height - 3, 0, sep)

        self.stdscr.attroff(curses.color_pair(1))

    def _progress_bar(self, completed: int, total: int) -> str:
        """Generate 12-char progress bar: [----------]"""
        if total == 0:
            return ""
        filled = (completed * 10) // total
        return "[" + "-" * filled + " " * (10 - filled) + "]"

    def _render_title(self, height: int, width: int):
        """Render the title bar with session count and progress."""
        if height < 3:
            return
        inner_width = width - 4
        if inner_width < 1:
            return

        title = "CLAUDEBOSS"
        count_str = ""
        if self.state and self.state.filtered:
            count_str = f"{len(self.state.filtered)} sessions"

        # Progress bar if summarizing
        progress_str = ""
        completed, total = getattr(self, '_summary_progress', (0, 0))
        if total > 0 and completed < total:
            progress_str = self._progress_bar(completed, total)

        # Calculate layout: title left-ish, progress + count right
        if inner_width > 50 and count_str:
            left_pad = 2
            self._safe_addstr(1, left_pad, f" {title} ", curses.color_pair(1) | curses.A_BOLD)

            # Progress bar (if active) then count
            right_content = f"{progress_str} {count_str}" if progress_str else count_str
            right_pad = width - len(right_content) - 3
            if progress_str:
                self._safe_addstr(1, right_pad, progress_str, curses.color_pair(3))
                self._safe_addstr(1, right_pad + len(progress_str) + 1, count_str, curses.color_pair(2))
            else:
                self._safe_addstr(1, right_pad, count_str, curses.color_pair(2))
        else:
            title_full = f" {title} "
            pad = max(2, (width - len(title_full)) // 2)
            self._safe_addstr(1, pad, title_full, curses.color_pair(1) | curses.A_BOLD)

    def _render_stats(self, height: int, width: int):
        """Render summary statistics."""
        if height < 6 or not self.state or not self.state.sessions:
            return

        inner_width = width - 4
        if inner_width < 10:
            return

        from datetime import datetime
        sessions = self.state.sessions
        now = datetime.now()
        oldest = min(s.mtime for s in sessions)
        age_days = (now - oldest).days

        recent = [s for s in sessions if (now - s.mtime).days < 7]
        per_day = len(recent) / 7 if recent else 0

        work = sum(1 for s in self.state.filtered if s.category == "professional")
        personal = len(self.state.filtered) - work

        # Sort/filter indicators
        sort_str = "TIME" if self.state.sort_mode == SortMode.MTIME else "SIZE"
        filter_map = {
            CategoryFilter.BOTH: "ALL",
            CategoryFilter.PERSONAL: "PER",
            CategoryFilter.PROFESSIONAL: "WRK",
        }
        filter_str = filter_map[self.state.category_filter]

        # Build stats string based on available width
        if inner_width > 60:
            stats = f"{len(self.state.filtered)} sessions │ {work}W/{personal}P │ {age_days}d old │ ~{per_day:.0f}/day │ [{sort_str}] [{filter_str}]"
        elif inner_width > 40:
            stats = f"{len(self.state.filtered)} │ {work}W/{personal}P │ [{sort_str}] [{filter_str}]"
        else:
            stats = f"{len(self.state.filtered)} [{sort_str}]"

        self._safe_addstr(3, 2, stats, curses.color_pair(2), inner_width)

        # Column headers - dynamic widths (order: SLUG, PATH, SUMMARY, WHEN, SIZE, CAT)
        # Indent by 2 to align with data rows (which have "> " marker space)
        if height > 6 and inner_width > 4:
            max_path = self._max_path_len()
            cols = self._get_column_widths(inner_width - 2, max_path)
            if cols:
                parts = []
                if cols.get('slug', 0) > 0:
                    parts.append(f"{'SLUG':<{cols['slug']}}")
                if cols.get('path', 0) > 0:
                    parts.append(f"{'DIR':<{cols['path']}}")
                if cols.get('summary', 0) > 0:
                    parts.append(f"{'SUMMARY':<{cols['summary']}}")
                if cols.get('when', 0) > 0:
                    parts.append(f"{'WHEN':<{cols['when']}}")
                if cols.get('size', 0) > 0:
                    parts.append(f"{'SIZE':<{cols['size']}}")
                if cols.get('cat', 0) > 0:
                    parts.append(f"{'C':<{cols['cat']}}")
                col_header = " ".join(parts)
                self._safe_addstr(4, 4, col_header, curses.A_DIM | curses.A_UNDERLINE, inner_width - 2)

    def _get_column_widths(self, inner_width: int, max_path_len: int = 12) -> dict[str, int]:
        """Calculate column widths based on available space.

        Order: SLUG, DIR, SUMMARY, WHEN/SIZE (sort column), CAT
        SUMMARY gets 50% of width minimum. SLUG is always 5 chars.
        DIR adapts to longest dir_name in list (max 15, min 8).
        The current sort column (WHEN or SIZE) is always shown (min 4 chars + 1 space).
        """
        if inner_width < 4:
            return {}

        w = inner_width
        slug_w = 5  # Always 5 chars
        path_w = max(8, min(max_path_len, 15))  # Cap at 15, min 8
        sort_w = 4  # Minimum for sort column: "30d" or "999M"

        # Determine which sort column to show
        sort_by_time = self.state.sort_mode == SortMode.MTIME if self.state else True
        sort_key = 'when' if sort_by_time else 'size'

        # Minimum layout: slug(5) + sort(4) + 1 space = 10
        if w < 10:
            return {'slug': min(slug_w, w)}

        # Always include sort column
        if w < 20:
            # slug + sort only
            return {'slug': slug_w, sort_key: min(sort_w, w - slug_w - 1)}

        if w >= 100:
            # slug(5) + path(dynamic) + summary(50%) + when(8) + size(6) + cat(1) + 5 spaces
            fixed = slug_w + path_w + 8 + 6 + 1 + 5
            summary_w = w - fixed
            if summary_w >= w // 2:
                cols = {'slug': slug_w, 'path': path_w, 'summary': summary_w, 'when': 8, 'size': 6, 'cat': 1}
            else:
                # Not enough room, shrink path
                summary_w = w // 2
                path_w = w - slug_w - summary_w - 8 - 6 - 1 - 5
                if path_w >= 8:
                    cols = {'slug': slug_w, 'path': path_w, 'summary': summary_w, 'when': 8, 'size': 6, 'cat': 1}
                else:
                    cols = {'slug': slug_w, 'summary': w - slug_w - 8 - 6 - 3, 'when': 8, 'size': 6}
        elif w >= 80:
            # slug(5) + path(dynamic) + summary(50%) + sort(8) + 3 spaces
            fixed = slug_w + path_w + 8 + 3
            summary_w = w - fixed
            if summary_w >= w // 2:
                cols = {'slug': slug_w, 'path': path_w, 'summary': summary_w, sort_key: 8}
            else:
                summary_w = w // 2
                path_w = w - slug_w - summary_w - 8 - 3
                if path_w >= 8:
                    cols = {'slug': slug_w, 'path': path_w, 'summary': summary_w, sort_key: 8}
                else:
                    cols = {'slug': slug_w, 'summary': w - slug_w - 8 - 2, sort_key: 8}
        elif w >= 50:
            # slug(5) + path(dynamic) + summary + sort(4) + 3 spaces
            fixed = slug_w + path_w + sort_w + 3
            summary_w = w - fixed
            if summary_w >= w // 3:
                cols = {'slug': slug_w, 'path': path_w, 'summary': summary_w, sort_key: sort_w}
            else:
                # Drop path
                summary_w = w - slug_w - sort_w - 2
                cols = {'slug': slug_w, 'summary': summary_w, sort_key: sort_w}
        elif w >= 30:
            # slug(5) + summary + sort(4) + 2 spaces
            summary_w = w - slug_w - sort_w - 2
            cols = {'slug': slug_w, 'summary': summary_w, sort_key: sort_w}
        else:
            # slug(5) + sort(4) + 1 space
            cols = {'slug': slug_w, sort_key: sort_w}

        return cols

    def _max_path_len(self) -> int:
        """Get the length of the longest dir_name in filtered sessions."""
        if not self.state or not self.state.filtered:
            return 12
        return max(len(s.dir_name) for s in self.state.filtered)

    def _render_sessions(self, height: int, width: int):
        """Render the session list."""
        inner_width = width - 4
        if height < 8 or inner_width < 2:
            return

        if not self.state or not self.state.filtered:
            self._safe_addstr(5, 2, "No sessions", curses.A_DIM, inner_width)
            return

        max_path = self._max_path_len()
        cols = self._get_column_widths(inner_width - 2, max_path)
        list_start = 5
        list_height = max(1, height - 8)
        visible_count = min(list_height, len(self.state.filtered) - self.state.scroll_offset)

        for i in range(visible_count):
            idx = self.state.scroll_offset + i
            if idx >= len(self.state.filtered):
                break

            session = self.state.filtered[idx]
            row = list_start + i
            is_selected = idx == self.state.cursor

            # Build line using same column widths as header (order: SLUG, PATH, SUMMARY, WHEN, SIZE, CAT)
            parts = []
            if cols.get('slug', 0) > 0:
                slug = session.slug[:cols['slug']]
                parts.append(f"{slug:<{cols['slug']}}")
            if cols.get('path', 0) > 0:
                col_w = cols['path']
                dir_name = session.dir_name
                if len(dir_name) > col_w:
                    # Truncate with ellipsis: first (col_w-3) chars + "..."
                    dir_name = dir_name[:col_w - 3] + "..."
                parts.append(f"{dir_name:<{col_w}}")
            thinking_anim = None
            if cols.get('summary', 0) > 0:
                if session.last_summary:
                    summary = session.last_summary[:cols['summary']]
                else:
                    summary = "Thinking..."
                    # Track animation phase per session, advance randomly for staggered effect
                    phase = self._thinking_phase.get(session.uuid, hash(session.uuid) % 22)
                    if random.random() < 0.3:
                        phase = (phase + 1) % 22
                    self._thinking_phase[session.uuid] = phase
                    # Calculate column offset for summary within the line
                    summary_offset = 0
                    if cols.get('slug', 0) > 0:
                        summary_offset += cols['slug'] + 1
                    if cols.get('path', 0) > 0:
                        summary_offset += cols['path'] + 1
                    thinking_anim = (summary_offset, phase)
                parts.append(f"{summary:<{cols['summary']}}")
            if cols.get('when', 0) > 0:
                when = session.mtime_display[:cols['when']]
                parts.append(f"{when:<{cols['when']}}")
            if cols.get('size', 0) > 0:
                size = session.context_display[:cols['size']]
                parts.append(f"{size:<{cols['size']}}")
            if cols.get('cat', 0) > 0:
                cat = "W" if session.category == "professional" else "P"
                parts.append(cat)

            line = " ".join(parts)

            if is_selected:
                # Fill entire row with selection color, add marker
                fill = " " * inner_width
                self._safe_addstr(row, 2, fill, curses.color_pair(6))
                marker = "▶" if session.is_active else ">"
                self._safe_addstr(row, 2, marker, curses.color_pair(6) | curses.A_BOLD)
                self._safe_addstr(row, 4, line, curses.color_pair(6) | curses.A_BOLD, inner_width - 2)
            else:
                # Active sessions get bright green, others get category color
                if session.is_active:
                    color = curses.color_pair(8) | curses.A_BOLD
                    marker = "▶"
                    self._safe_addstr(row, 2, marker, color)
                else:
                    color = curses.color_pair(3) if session.category == "professional" else curses.color_pair(4)
                self._safe_addstr(row, 4, line, color, inner_width - 2)
                # Render "Thinking..." with brightness wave animation
                if thinking_anim:
                    text = "Thinking..."
                    offset, phase = thinking_anim
                    x_base = 4 + offset
                    # 25% of sessions animate right-to-left
                    reverse = (hash(session.uuid) % 4) == 0
                    for ci, ch in enumerate(text):
                        # Phase 0-10: bold wave; 11-21: dim wave
                        idx = (len(text) - 1 - ci) if reverse else ci
                        if phase <= 10:
                            attr = curses.A_BOLD if idx < phase else curses.A_DIM
                        else:
                            attr = curses.A_DIM if idx < (phase - 11) else curses.A_BOLD
                        try:
                            self.stdscr.addch(row, x_base + ci, ch, color | attr)
                        except curses.error:
                            pass

    def _render_help(self, height: int, width: int):
        """Render help/control legend in footer bar style."""
        if height < 4:
            return
        inner_width = width - 4
        if inner_width < 10:
            return

        # Build help based on available width
        if inner_width > 70:
            help_items = [("j/k", "nav"), ("l", "open"), ("h", "quit"), ("s", "sort"), ("c", "filter"), ("R", "regen"), ("?", "help")]
        elif inner_width > 50:
            help_items = [("j/k", "nav"), ("l", "open"), ("h", "quit"), ("s", "sort"), ("R", "regen")]
        elif inner_width > 30:
            help_items = [("jk", "nav"), ("l", "open"), ("h", "quit")]
        else:
            help_items = [("l", "open"), ("h", "quit")]

        # Format: key in dim, action normal, separated by spaces
        help_parts = []
        for key, action in help_items:
            help_parts.append(f"{key}:{action}")
        help_line = "  ".join(help_parts)

        # Position info
        if self.state and self.state.filtered:
            pos = f"[{self.state.cursor + 1}/{len(self.state.filtered)}]"
        else:
            pos = ""

        # Calculate space for help vs position
        pos_space = len(pos) + 2 if pos else 0
        help_space = inner_width - pos_space

        self._safe_addstr(height - 2, 2, help_line, curses.color_pair(7) | curses.A_DIM, help_space)

        if pos:
            pos_x = width - len(pos) - 2
            if pos_x > 2:
                self._safe_addstr(height - 2, pos_x, pos, curses.color_pair(2))

    def handle_key(self, key: int) -> Optional[str]:
        """Handle keyboard input. Returns action string or None.

        Navigation follows ncdu/vim conventions:
        - j/k/↑/↓: cursor movement
        - l/Enter/→: select/forward (enter detail view)
        - h/←: back (quit at root level)
        - q: quit
        """
        if not self.state:
            return None

        # Vertical movement
        if key in (ord("j"), curses.KEY_DOWN):
            self.state.move_cursor(1)
        elif key in (ord("k"), curses.KEY_UP):
            self.state.move_cursor(-1)
        elif key == curses.KEY_PPAGE or key == 21:  # Ctrl+U
            self.state.move_cursor(-self.state.page_size)
        elif key == curses.KEY_NPAGE or key == 4:  # Ctrl+D
            self.state.move_cursor(self.state.page_size)
        elif key in (ord("g"), curses.KEY_HOME):
            self.state.cursor = 0
            self.state.scroll_offset = 0
        elif key in (ord("G"), curses.KEY_END):
            self.state.cursor = len(self.state.filtered) - 1
            self.state._adjust_scroll()

        # Forward: l/Enter/→ = select session, go to detail
        elif key in (ord("l"), ord("\n"), curses.KEY_ENTER, curses.KEY_RIGHT, 10, 13):
            return "select"

        # Back: h/← = quit (at root level)
        elif key in (ord("h"), curses.KEY_LEFT, ord("q"), 27):  # 27 = ESC
            return "quit"

        # Actions
        elif key == ord("s"):
            self.state.toggle_sort()
        elif key == ord("c"):
            self.state.cycle_category()
        elif key == ord("?"):
            return "help"
        elif key == ord("m"):
            return "menu"
        elif key == ord("R"):
            return "regenerate"

        return None


class Menu:
    """Generic menu component - BBS style."""

    def __init__(self, stdscr, title: str, items: list[tuple[str, str, callable]]):
        self.stdscr = stdscr
        self.title = title
        self.items = items
        self.cursor = 0

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

    def render(self):
        """Render the menu."""
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        inner_width = width - 4

        if width < MIN_WIDTH or height < MIN_HEIGHT:
            self.stdscr.refresh()
            return

        # Border
        self.stdscr.attron(curses.color_pair(1))
        top = BOX_TL + BOX_H * max(0, width - 2) + BOX_TR
        self._safe_addstr(0, 0, top)
        for y in range(1, height - 1):
            self._safe_addstr(y, 0, BOX_V)
            if width > 1:
                self._safe_addstr(y, width - 1, BOX_V)
        if height > 1:
            bottom = BOX_BL + BOX_H * max(0, width - 3) + BOX_BR
            self._safe_addstr(height - 1, 0, bottom)
        if height > 4:
            sep = BOX_L + BOX_H * max(0, width - 2) + BOX_R
            self._safe_addstr(2, 0, sep)
        self.stdscr.attroff(curses.color_pair(1))

        # Title
        if height > 2 and inner_width > 5:
            title = self.title if len(self.title) < inner_width else self.title[:inner_width - 2]
            pad = max(2, (width - len(title) - 2) // 2)
            self._safe_addstr(1, pad, f" {title} ", curses.color_pair(1) | curses.A_BOLD)

        # Items
        if height > 5 and inner_width > 10:
            max_items = height - 6
            for i, (key, label, _) in enumerate(self.items[:max_items]):
                row = 4 + i
                if row >= height - 2:
                    break
                prefix = "▶" if i == self.cursor else " "
                line = f"{prefix} [{key}] {label}"

                if i == self.cursor:
                    fill = " " * inner_width
                    self._safe_addstr(row, 2, fill, curses.color_pair(6))
                    self._safe_addstr(row, 2, line, curses.color_pair(6), inner_width)
                else:
                    self._safe_addstr(row, 2, line, 0, inner_width)

        # Help
        if height > 3 and inner_width > 15:
            help_row = height - 2
            if inner_width > 35:
                help_text = "j/k:navigate  Enter:select  q:back"
            else:
                help_text = "jk:nav Enter:sel q:back"
            self._safe_addstr(help_row, 2, help_text, curses.color_pair(7), inner_width)

        self.stdscr.refresh()

    def handle_key(self, key: int) -> Optional[callable]:
        """Handle input. Returns callback if item selected.

        Navigation follows ncdu/vim conventions:
        - j/k/↑/↓: cursor movement
        - l/Enter/→: select
        - h/←/q/ESC: back
        """
        # Vertical movement
        if key in (ord("j"), curses.KEY_DOWN):
            self.cursor = (self.cursor + 1) % len(self.items)
        elif key in (ord("k"), curses.KEY_UP):
            self.cursor = (self.cursor - 1) % len(self.items)

        # Forward: select item
        elif key in (ord("l"), ord("\n"), curses.KEY_ENTER, curses.KEY_RIGHT, 10, 13):
            return self.items[self.cursor][2]

        # Back: return to list
        elif key in (ord("h"), curses.KEY_LEFT, ord("q"), 27):  # 27 = ESC
            return lambda: None

        # Hotkeys
        else:
            for i, (hotkey, _, callback) in enumerate(self.items):
                if key == ord(hotkey.lower()):
                    return callback

        return None
