"""IST time helpers — Pi's day and week boundaries are IST, not server-local.

"Today" refreshes at IST midnight, the week starts Monday (IST), and
milestone timestamps show IST clock time — regardless of where the bot
runs (Railway/UTC, local Windows, etc.).
"""

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def ist_now() -> datetime:
    return datetime.now(IST)


def ist_date() -> str:
    """Current date in IST — YYYY-MM-DD (refreshes at IST midnight)."""
    return ist_now().date().isoformat()


def ist_clock() -> str:
    """Current IST clock time — HH:MM (milestone timestamps)."""
    return ist_now().strftime("%H:%M")


def ist_monday() -> str:
    """Monday of the current IST week — YYYY-MM-DD."""
    today = ist_now().date()
    return (today - timedelta(days=today.weekday())).isoformat()


def ist_month_start() -> str:
    """First day of the current IST calendar month — YYYY-MM-DD."""
    return ist_now().date().replace(day=1).isoformat()


def utcnow_iso() -> str:
    """Aware UTC timestamp as ISO text — safe for lexicographic compare."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
