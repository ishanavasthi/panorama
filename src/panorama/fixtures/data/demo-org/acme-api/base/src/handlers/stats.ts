import type { Request, Response } from "express";

// Aggregate counts for the reporting clients. This is the only endpoint that
// exposes organisation-wide totals rather than a single link.
export function handleStats(_req: Request, res: Response): void {
  res.json({
    total_links: 0,
    active_links: 0,
    generated_at: new Date().toISOString(),
  });
}
