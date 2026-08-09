"""Plain-text report rendering.

Everything here is local presentation — column widths, padding and separators.
It deliberately shares no vocabulary with any other repository, which is what
makes it a fair subject for a formatting-only control change.
"""


def column_widths(rows):
    """Widest cell per column across every row."""
    widths = []
    for row in rows:
        for position, cell in enumerate(row):
            if position >= len(widths):
                widths.append(0)
            widths[position] = max(widths[position], len(cell))
    return widths


def render_table(rows):
    """Render rows as an aligned table with a rule under the first row."""
    if not rows:
        return ""
    widths = column_widths(rows)
    rule = "-+-".join("-" * width for width in widths)
    padded = [" | ".join(cell.ljust(width) for cell, width in zip(row, widths)) for row in rows]
    return "\n".join([padded[0], rule, *padded[1:]])
