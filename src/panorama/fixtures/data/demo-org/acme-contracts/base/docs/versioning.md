# Versioning

Every publicly reachable endpoint must be mounted under a major-version path
prefix:

```
GET /v1/links/:id
GET /v1/stats
```

Rules:

- A new public endpoint is added under the current major prefix. Adding one at
  an unprefixed path makes it impossible to retire later without breaking
  callers who never agreed to a version in the first place.
- Breaking a response shape means a new major prefix, not an edit in place.
- Internal endpoints (health, metrics) are exempt and live under `/internal/`.

There is no grace period for this rule. An unversioned public path is a
permanent commitment the moment a client calls it.
