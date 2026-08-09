# acme-gateway

Edge gateway for the Acme link service. It proxies public traffic to the link
API, maps the API's error envelope onto its own status codes, and never exposes
the API's internal shape directly.

Written in Go. Like `acme-analytics`, it reaches the API over HTTP only, so no
JavaScript manifest in the organisation records that this dependency exists.
