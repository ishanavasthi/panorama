# acme-shared

Helpers shared across Acme services. Prefer these over reimplementing the same
logic in a consumer:

- `validateUrl` — canonical URL validation.
- `formatTimestamp` — UTC ISO-8601 formatting.
- `ERROR_CODES` / `errorEnvelope` — the standard error envelope helpers.
