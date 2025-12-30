"""claudeboss - Claude Code session browser.

Navigate and manage Claude Code sessions from the terminal.
"""

import os
import sys


def _setup_terminal_env():
    """Configure TERM/TERMINFO for curses compatibility across platforms.

    Handles:
    - Kitty terminal (xterm-kitty needs /usr/lib/kitty/terminfo)
    - Standard terminals (xterm-256color in /usr/share/terminfo)
    - macOS paths (/opt/homebrew/share/terminfo, /usr/share/terminfo)
    - WSL/Linux distros (Ubuntu, Fedora, Arch all use /usr/share/terminfo)
    """
    term = os.environ.get('TERM', '')

    # Known terminfo locations by priority
    terminfo_paths = [
        '/usr/lib/kitty/terminfo',      # Kitty's custom terminfo
        '/usr/share/terminfo',           # Standard Linux (Ubuntu, Fedora, Arch, WSL)
        '/lib/terminfo',                 # Some minimal systems
        '/opt/homebrew/share/terminfo',  # macOS Homebrew
        '/usr/local/share/terminfo',     # macOS/BSD manual installs
    ]

    def _terminfo_has(terminfo_dir: str, term_name: str) -> bool:
        """Check if terminfo directory has definition for term_name."""
        if not term_name or not os.path.isdir(terminfo_dir):
            return False
        # terminfo files are stored as first-char/term-name (e.g., x/xterm-256color)
        first_char = term_name[0]
        term_file = os.path.join(terminfo_dir, first_char, term_name)
        return os.path.exists(term_file)

    # If TERM is set, try to find the right TERMINFO for it
    if term:
        # Check if current TERMINFO (if set) works
        current_terminfo = os.environ.get('TERMINFO', '')
        if current_terminfo and _terminfo_has(current_terminfo, term):
            return  # Current setup works

        # Search for the right terminfo for this TERM
        for path in terminfo_paths:
            if _terminfo_has(path, term):
                os.environ['TERMINFO'] = path
                return

        # TERM is set but we can't find its terminfo - fall back to xterm-256color
        # This handles exotic terminals that don't have their terminfo installed
        for path in terminfo_paths:
            if _terminfo_has(path, 'xterm-256color'):
                os.environ['TERM'] = 'xterm-256color'
                os.environ['TERMINFO'] = path
                return

    # No TERM set - use xterm-256color (universally available)
    os.environ['TERM'] = 'xterm-256color'
    for path in terminfo_paths:
        if _terminfo_has(path, 'xterm-256color'):
            os.environ['TERMINFO'] = path
            return

    # Last resort - hope /usr/share/terminfo exists
    os.environ['TERMINFO'] = '/usr/share/terminfo'


_setup_terminal_env()

import curses
import subprocess
import threading

from .active_detector import refresh_active_status
from .detail import SessionDetailView
from .session import Session, load_sessions
from .summarizer import summarize_single, load_cache, save_cache
from .ui import Menu, SessionListView


class App:
    """Main application controller."""

    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.view = SessionListView(stdscr)
        self.detail_view = SessionDetailView(stdscr)
        self.running = True
        self.mode = "list"
        self.menu: Menu | None = None
        self._summarizer_thread: threading.Thread | None = None
        self._stop_summarizer = threading.Event()
        self._summary_progress: tuple[int, int] = (0, 0)
        self._active_watcher_thread: threading.Thread | None = None
        self._stop_active_watcher = threading.Event()

        curses.curs_set(0)
        self.stdscr.keypad(True)
        self.stdscr.timeout(100)
        try:
            curses.cbreak()
            self.stdscr.immedok(False)
            self.stdscr.leaveok(True)
        except curses.error:
            pass

    def run(self):
        """Main event loop."""
        sessions = load_sessions()
        self._load_cached_summaries(sessions)
        refresh_active_status(sessions)
        self.view.set_sessions(sessions)

        self._summarizer_thread = threading.Thread(
            target=self._background_summarize, daemon=True
        )
        self._summarizer_thread.start()

        self._active_watcher_thread = threading.Thread(
            target=self._background_active_watcher, daemon=True
        )
        self._active_watcher_thread.start()

        while self.running:
            try:
                self._render()
                self._handle_input()
            except KeyboardInterrupt:
                break

        self._stop_summarizer.set()
        self._stop_active_watcher.set()

    def _load_cached_summaries(self, sessions: list[Session]):
        """Pre-populate summaries from cache."""
        cache = load_cache()
        for s in sessions:
            if s.context_start or s.context_end:
                if s.uuid in cache:
                    entry = cache[s.uuid]
                    if isinstance(entry, dict):
                        s.last_summary = entry.get("summary", "")
                    else:
                        s.last_summary = entry

    def _background_summarize(self):
        """Summarize sessions in background."""
        if not self.view.state:
            return
        to_summarize = [s for s in self.view.state.sessions if not s.last_summary]
        total = len(to_summarize)
        self._summary_progress = (0, total)

        for idx, session in enumerate(to_summarize):
            if self._stop_summarizer.is_set():
                break
            summarize_single(session)
            self._summary_progress = (idx + 1, total)

        self._summary_progress = (0, 0)

    def _background_active_watcher(self):
        """Periodically update active session status."""
        import time
        while not self._stop_active_watcher.is_set():
            time.sleep(5)
            if self._stop_active_watcher.is_set():
                break
            if self.view.state:
                refresh_active_status(self.view.state.sessions)

    def _render(self):
        """Render current mode."""
        if self.mode == "list":
            self.view.render(self._summary_progress)
        elif self.mode == "menu":
            if self.menu:
                self.menu.render()
        elif self.mode == "detail":
            self.detail_view.render()
        elif self.mode == "help":
            self._render_help()

    def _handle_input(self):
        """Handle input for current mode."""
        try:
            key = self.stdscr.getch()
        except curses.error:
            return

        if key == -1:
            return

        if key == curses.KEY_RESIZE:
            self._handle_resize()
            return

        if self.mode == "list":
            action = self.view.handle_key(key)
            if action == "quit":
                self.running = False
            elif action == "select":
                session = self.view.state.current_session if self.view.state else None
                if session:
                    self.detail_view.set_session(session)
                    self.mode = "detail"
            elif action == "help":
                self.mode = "help"
            elif action == "menu":
                self._open_menu()
            elif action == "regenerate":
                self._regenerate_summary()

        elif self.mode == "menu":
            if self.menu:
                callback = self.menu.handle_key(key)
                if callback:
                    callback()
                    self.mode = "list"

        elif self.mode == "detail":
            action = self.detail_view.handle_key(key)
            if action == "back":
                self.mode = "list"
            elif action == "resume":
                self._detail_resume()
            elif action and action.startswith("fork:"):
                fork_dir = action[5:]
                self._detail_fork(fork_dir)

        elif self.mode == "help":
            if key in (ord("h"), curses.KEY_LEFT, ord("q"), 27, ord("\n"), 10, 13):
                self.mode = "list"

    def _handle_resize(self):
        """Handle terminal resize."""
        curses.update_lines_cols()
        self.view.update_page_size()
        self.stdscr.clear()

    def _open_menu(self):
        """Open the main menu."""
        items = [
            ("s", "Settings", self._menu_settings),
            ("h", "Hooks", self._menu_hooks),
            ("k", "Skills", self._menu_skills),
            ("m", "MCP Servers", self._menu_mcps),
            ("p", "Projects", self._menu_projects),
            ("r", "Reload Sessions", self._reload_sessions),
        ]
        self.menu = Menu(self.stdscr, "CLAUDEBOSS MENU", items)
        self.mode = "menu"

    def _menu_settings(self):
        self._show_message("Settings viewer coming soon...")

    def _menu_hooks(self):
        self._show_message("Hooks viewer coming soon...")

    def _menu_skills(self):
        self._show_message("Skills viewer coming soon...")

    def _menu_mcps(self):
        self._show_message("MCP servers viewer coming soon...")

    def _menu_projects(self):
        self._show_message("Projects viewer coming soon...")

    def _reload_sessions(self):
        """Reload sessions from disk."""
        sessions = load_sessions()
        self.view.set_sessions(sessions)

    def _regenerate_summary(self):
        """Clear and regenerate summary for current session."""
        if not self.view.state:
            return
        session = self.view.state.current_session
        if not session:
            return

        cache = load_cache()
        if session.uuid in cache:
            del cache[session.uuid]
            save_cache(cache)

        session.last_summary = ""

        def regen():
            summarize_single(session)
        threading.Thread(target=regen, daemon=True).start()

    def _show_message(self, msg: str):
        """Show a temporary message."""
        height, width = self.stdscr.getmaxyx()
        self.stdscr.clear()
        self.stdscr.addstr(height // 2, (width - len(msg)) // 2, msg)
        self.stdscr.refresh()
        self.stdscr.timeout(-1)
        self.stdscr.getch()
        self.stdscr.timeout(100)

    def _render_help(self):
        """Render help screen."""
        self.stdscr.clear()
        height, width = self.stdscr.getmaxyx()

        help_text = [
            "CLAUDEBOSS - Claude Code Session Browser",
            "",
            "NAVIGATION (vim/ncdu style)",
            "  j/k, up/down  Move cursor up/down",
            "  l, Enter      Open session detail",
            "  h, q          Back / quit",
            "  Ctrl+U/D      Page up/down",
            "  g/G           Go to top/bottom",
            "",
            "IN DETAIL VIEW",
            "  j/k           Scroll content",
            "  l, Enter      Resume session in kitty",
            "  f             Fork session to new directory",
            "  h, q          Back to list",
            "",
            "ACTIONS",
            "  s             Toggle sort: time / size",
            "  c             Cycle filter: all / personal / work",
            "  R             Regenerate summary",
            "  m             Open menu",
            "  ?             Show this help",
            "",
            "Press h to return...",
        ]

        self.stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
        self.stdscr.addstr(1, 2, help_text[0])
        self.stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)

        for i, line in enumerate(help_text[1:], start=3):
            if i >= height - 1:
                break
            self.stdscr.addstr(i, 2, line[:width - 4])

        self.stdscr.refresh()

    def _resolve_session_path(self, session: Session) -> str | None:
        """Resolve session working directory."""
        if session.cwd and os.path.isdir(session.cwd):
            return session.cwd
        fallback = "/" + session.project_path.replace("-", "/")
        return fallback if os.path.isdir(fallback) else None

    def _detail_resume(self):
        """Resume the session shown in detail view."""
        if not self.detail_view.state:
            return
        session = self.detail_view.state.session
        self._resume_session_by_uuid(session)
        self.mode = "list"

    def _resume_session_by_uuid(self, session: Session):
        """Open a new kitty terminal and resume the session."""
        if not session:
            return

        path = self._resolve_session_path(session)
        if not path:
            self._show_message("Session directory not found")
            return

        # Use bash -c with exec to run claude, then drop to shell on exit
        # This keeps the terminal open after claude exits (Ctrl+C)
        cmd = f'claude --resume {session.uuid}; exec bash'

        try:
            subprocess.Popen([
                "kitty",
                "--directory", path,
                "--", "bash", "-c", cmd
            ], start_new_session=True)
        except FileNotFoundError:
            self._show_message("kitty not found")
        except Exception as e:
            self._show_message(f"Failed to launch: {e}")

    def _detail_fork(self, fork_dir: str):
        """Fork the session shown in detail view to a new directory."""
        if not self.detail_view.state:
            return
        session = self.detail_view.state.session
        self._fork_session(session, fork_dir)
        self.mode = "list"

    def _fork_session(self, session: Session, fork_dir: str):
        """Open a new kitty terminal in fork_dir and resume the session there."""
        if not session:
            return

        # Validate fork directory
        fork_dir = os.path.expanduser(fork_dir)
        if not os.path.isdir(fork_dir):
            self._show_message(f"Directory not found: {fork_dir}")
            return

        # Use bash -c with exec to run claude, then drop to shell on exit
        cmd = f'claude --resume {session.uuid}; exec bash'

        try:
            subprocess.Popen([
                "kitty",
                "--directory", fork_dir,
                "--", "bash", "-c", cmd
            ], start_new_session=True)
        except FileNotFoundError:
            self._show_message("kitty not found")
        except Exception as e:
            self._show_message(f"Failed to launch: {e}")


def _run_app(stdscr):
    """Entry point for curses wrapper."""
    app = App(stdscr)
    app.run()


def check_terminal():
    """Check if terminal environment supports curses."""
    if not sys.stdin.isatty():
        return False, "Not running in a TTY"

    term = os.environ.get('TERM', '')
    if not term:
        return False, "TERM environment variable not set"

    terminfo = os.environ.get('TERMINFO', '/usr/share/terminfo')
    if not os.path.isdir(terminfo):
        return False, f"TERMINFO directory not found: {terminfo}"

    try:
        curses.setupterm()
    except curses.error as e:
        return False, f"curses.setupterm() failed: {e}"

    return True, None


def main():
    """Main entry point."""
    ok, err = check_terminal()
    if not ok:
        print(f"Terminal check failed: {err}")
        print()
        print("Run with explicit environment:")
        print()
        print("  TERM=xterm-256color TERMINFO=/usr/share/terminfo claudeboss")
        print()
        sys.exit(1)

    curses.wrapper(_run_app)
