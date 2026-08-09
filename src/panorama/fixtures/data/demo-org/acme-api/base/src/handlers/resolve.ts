import type { Request, Response } from "express";

// Resolves a short code to its destination. A link we cannot serve is reported
// with the standard error envelope so clients can switch on `error.code`.
export function handleResolve(req: Request, res: Response): void {
  const link = lookupTarget(req.params.id);
  if (!link) {
    res.status(404).json({ error: { code: "not_found", message: "link not found" } });
    return;
  }
  res.json({ destination: link });
}

function lookupTarget(id: string): string | null {
  return id ? `https://example.com/${id}` : null;
}
