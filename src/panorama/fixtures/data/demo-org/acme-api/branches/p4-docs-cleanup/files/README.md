# acme-api

The link API for the Acme organisation. It owns the HTTP response shape that
clients such as `acme-web` depend on, and it must follow the organisation
conventions documented in `acme-contracts`.

## Endpoints

- `GET /links/:id` — returns a single link as JSON.
- `GET /resolve/:id` — resolves a short code to its destination.
- `GET /stats` — aggregate counts for reporting clients.

## Development

Run `npm install` to fetch dependencies, then start the server with
`npm run dev`. See `src/server.ts` for the route wiring.
