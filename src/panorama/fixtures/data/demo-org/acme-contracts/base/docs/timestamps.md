# Timestamps

All timestamps in API responses must be UTC ISO-8601, e.g.
`2024-01-01T12:00:00.000Z`, as produced by `new Date().toISOString()`.

Never emit locale-formatted or local-timezone timestamps (for example
`new Date().toLocaleString()`): they are ambiguous across regions and are not
machine-parseable.

## Per language

The organisation is polyglot, so the rule is spelled out per language along
with the idiom that violates it.

| Language | Use | Never use |
|---|---|---|
| TypeScript | `new Date().toISOString()` | `toLocaleString`, `toLocaleDateString` |
| Python | `datetime.now(timezone.utc).isoformat()` | `strftime("%c")`, naive `datetime.now()` |
| Go | `time.Now().UTC().Format(time.RFC3339)` | `time.RFC1123`, `time.Kitchen`, `time.ANSIC` |

The shared library exposes the format as `TIMESTAMP_FORMAT` so services that
must build the string by hand still agree on one layout.
