# acme-shared

Helpers shared across Acme services. Prefer these over reimplementing the same
logic in a consumer:

- `validateUrl` — canonical URL validation.
- `formatTimestamp` / `TIMESTAMP_FORMAT` — UTC ISO-8601 formatting.
- `ERROR_CODES` / `errorEnvelope` — the standard error envelope helpers.
- `retryWithBackoff` — the organisation's retry policy for upstream calls.
- `MAX_LINKS_PER_PAGE` / `DEFAULT_LINKS_PER_PAGE` — pagination limits.
