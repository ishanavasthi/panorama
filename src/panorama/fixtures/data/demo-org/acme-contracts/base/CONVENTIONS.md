# API conventions

All Acme HTTP services must follow these conventions. They are binding on every
endpoint, including new ones.

1. **Errors** use the standard envelope. See `docs/error-envelope.md`.
2. **Timestamps** in responses are UTC ISO-8601. See `docs/timestamps.md`.
3. **Versioning** is by major version in the path (`/v1/...`) once a service is
   public.
