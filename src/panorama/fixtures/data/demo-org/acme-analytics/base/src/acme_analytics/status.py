"""Link lifecycle states, mirrored from the link API's status enum.

The API owns these values. This module exists so the reporting code has one
place to change when the API's set of states changes — which it has no way of
learning about automatically, because nothing but HTTP connects the two.
"""

STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_ARCHIVED = "archived"

REPORTABLE_STATUSES = (STATUS_ACTIVE, STATUS_EXPIRED, STATUS_ARCHIVED)


def is_reportable(status):
    """Whether a link in this state should appear in a report."""
    return status in REPORTABLE_STATUSES
