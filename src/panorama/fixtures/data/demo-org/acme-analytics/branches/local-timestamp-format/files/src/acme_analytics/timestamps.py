"""Timestamp formatting for report output."""

from datetime import timezone

# UTC ISO-8601 with milliseconds, matching what the services emit.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def format_timestamp(moment):
    """Render a datetime as a UTC ISO-8601 string."""
    return moment.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)
