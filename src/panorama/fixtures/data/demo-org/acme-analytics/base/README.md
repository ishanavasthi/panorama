# acme-analytics

Reporting client for the Acme link service. It reads the link API over HTTP —
`GET /links/{id}` for a single link and `GET /stats` for aggregate counts — and
renders plain-text reports.

It is a Python package, so it appears in no JavaScript manifest anywhere in the
organisation. The only thing tying it to the API is the HTTP contract, which is
exactly why a change to that contract is easy to miss from the API side.
