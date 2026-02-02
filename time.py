from datetime import datetime, timedelta

# -----------------------------
# Format seconds into human-readable
# -----------------------------
def format_seconds(seconds: int) -> str:
    """
    Convert seconds to a human-readable string.
    Example: 3661 -> "1h 1m 1s"
    """
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


# -----------------------------
# Format timedelta
# -----------------------------
def format_timedelta(td: timedelta) -> str:
    """
    Format a timedelta object to human-readable string.
    """
    total_seconds = int(td.total_seconds())
    return format_seconds(total_seconds)


# -----------------------------
# Get current UTC timestamp
# -----------------------------
def now_utc() -> datetime:
    """
    Return current UTC datetime.
    """
    return datetime.utcnow()


# -----------------------------
# Calculate seconds until a future datetime
# -----------------------------
def seconds_until(future_time: datetime) -> int:
    """
    Returns the number of seconds from now until future_time.
    Returns 0 if already passed.
    """
    delta = future_time - now_utc()
    return max(int(delta.total_seconds()), 0)
