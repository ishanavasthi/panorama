# Error envelope

Every error response body must be a JSON object of the form:

```json
{ "error": { "code": "not_found", "message": "human readable detail" } }
```

- `code` is a stable, machine-readable string.
- `message` is human-readable and may change.

Never return a bare string as an error body. Clients parse `error.code` and
will break on a plain-text body.
