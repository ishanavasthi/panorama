# Timestamps

All timestamps in API responses must be UTC ISO-8601, e.g.
`2024-01-01T12:00:00.000Z`, as produced by `new Date().toISOString()`.

Never emit locale-formatted or local-timezone timestamps (for example
`new Date().toLocaleString()`): they are ambiguous across regions and are not
machine-parseable.
