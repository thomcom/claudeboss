"""Generate short summaries of session context using Claude CLI."""

import hashlib
import json
import subprocess
from pathlib import Path


CACHE_DIR = Path.home() / ".cache" / "claudeboss"
CACHE_FILE = CACHE_DIR / "summary_cache.json"


def _stable_hash(s: str) -> str:
    """Deterministic hash for cache keys."""
    return hashlib.md5(s.encode()).hexdigest()[:16]


def load_cache() -> dict:
    """Load cached summaries.

    Format: {uuid -> {hash, size, summary}}
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_cache(cache: dict[str, str]):
    """Save summaries to cache."""
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)


SUMMARY_PROMPT = """You are a title generator. Read the session transcript and output a PRODUCT or JOB TITLE.

Rules:
- Maximum 6 words, Title Case
- Plain text only - no markdown, no asterisks, no formatting
- MUST identify the core subject noun - the specific thing being worked on
- The subject noun should be prominent (e.g., project name, tool, system, API)
- No action verbs (Building, Fixing, Setting up, etc.)
- Name what was MADE or what JOB was done, not the action

Examples of subject nouns: AgentiCloud, NSPECT, Milvus, PLC, Emulator, VPN, SSH, Bluetooth
Good: Milvus Database Size Check, NSPECT API Integration, SSH Daemon Setup, Slack Export Tool
Bad: **Building** a browser, Fixing the API, Database work"""


def _call_haiku(prompt: str) -> str:
    """Call claude CLI with haiku model."""
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--tools", ""],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=30,
            cwd="/tmp"
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass
    return ""


def _extract_title(raw: str) -> str:
    """Extract clean title from LLM response."""
    if not raw:
        return ""
    lines = raw.split('\n')
    for line in reversed(lines):
        line = line.strip().replace('**', '').replace('*', '')
        if line and not line.startswith('Session') and not line.startswith('Shell') and len(line) < 80:
            words = line.split()
            if len(words) <= 8:
                return ' '.join(words[:6]).title()
    return ' '.join(raw.split('\n')[0].replace('**', '').replace('*', '').split()[:6]).title()


def _summarize_via_cli(context: str) -> str:
    """Call claude CLI in print mode to get summary."""
    prompt = f"""{SUMMARY_PROMPT}

SESSION TRANSCRIPT:
{context[:2000]}

TITLE:"""
    return _extract_title(_call_haiku(prompt))


def _summarize_delta(old_summary: str, new_tail: str) -> str:
    """Update summary based on new tail content, if needed."""
    prompt = f"""{SUMMARY_PROMPT}

The previous title was: {old_summary}

Based on the NEW WORK below, output an updated title ONLY if the focus has shifted.
If the new work is just continuation of the same topic, output the same title.

NEW WORK:
{new_tail[:2000]}

TITLE:"""
    return _extract_title(_call_haiku(prompt))


def summarize_context(context_start: str, context_end: str, session_uuid: str) -> str:
    """Generate a max 6 word ALL CAPS title for the session."""
    if not context_start and not context_end:
        return ""

    cache = load_cache()
    cache_key = f"{session_uuid}:{_stable_hash(context_start + context_end)}"
    if cache_key in cache:
        return cache[cache_key]

    context = f"START:\n{context_start[:1000]}\n\nEND:\n{context_end[:1000]}"
    summary = _summarize_via_cli(context)

    if summary:
        cache[cache_key] = summary
        save_cache(cache)

    return summary


def summarize_single(session) -> None:
    """Summarize a single session, updating session.last_summary in place.

    Uses delta summarization for active sessions - if we have an old summary
    and the session has grown significantly (>10KB new content), ask LLM to
    update if needed rather than regenerating from scratch.

    Cache format: {uuid -> {"hash": content_hash, "size": context_size, "summary": text}}
    """
    if not session.context_start and not session.context_end:
        session.last_summary = ""
        return

    cache = load_cache()
    content_hash = _stable_hash((session.context_start or '') + (session.context_end or ''))
    current_size = session.context_size

    # Check for exact cache hit (content unchanged)
    cache_key = session.uuid
    if cache_key in cache:
        entry = cache[cache_key]
        # Handle both old format (string) and new format (dict)
        if isinstance(entry, str):
            # Old format - migrate it
            cache[cache_key] = {"hash": content_hash, "size": current_size, "summary": entry}
            save_cache(cache)
            session.last_summary = entry
            return
        elif entry.get("hash") == content_hash:
            session.last_summary = entry["summary"]
            return
        else:
            # Hash changed - check if grown enough to re-summarize
            old_size = entry.get("size", 0)
            old_summary = entry["summary"]

            # Only re-summarize if grown by >10KB (~10 messages worth)
            if current_size - old_size < 10000:
                session.last_summary = old_summary
                return

            # Delta update - just send the tail
            summary = _summarize_delta(old_summary, session.context_end or '')
            if summary:
                cache[cache_key] = {"hash": content_hash, "size": current_size, "summary": summary}
                save_cache(cache)
                session.last_summary = summary
            else:
                session.last_summary = old_summary
            return

    # No cache entry - full summarization
    context = f"START:\n{(session.context_start or '')[:1000]}\n\nEND:\n{(session.context_end or '')[:1000]}"
    summary = _summarize_via_cli(context)

    if summary:
        cache[cache_key] = {"hash": content_hash, "size": current_size, "summary": summary}
        save_cache(cache)
        session.last_summary = summary
    else:
        session.last_summary = ""


def summarize_sessions(sessions: list, max_concurrent: int = 10) -> list:
    """Add summaries to sessions that don't have them cached.

    Uses claude CLI in print mode with haiku model.
    """
    cache = load_cache()

    # First pass: check cache and identify what needs summarizing
    to_summarize = []
    for s in sessions:
        if not s.context_start and not s.context_end:
            s.last_summary = ""
            continue
        cache_key = f"{s.uuid}:{_stable_hash((s.context_start or '') + (s.context_end or ''))}"
        if cache_key in cache:
            s.last_summary = cache[cache_key]
        else:
            to_summarize.append((s, cache_key))

    # Second pass: summarize uncached via CLI
    for s, cache_key in to_summarize[:max_concurrent]:
        context = f"START:\n{(s.context_start or '')[:1000]}\n\nEND:\n{(s.context_end or '')[:1000]}"
        summary = _summarize_via_cli(context)
        if summary:
            cache[cache_key] = summary
            s.last_summary = summary
        else:
            s.last_summary = ""

    if to_summarize:
        save_cache(cache)

    return sessions
