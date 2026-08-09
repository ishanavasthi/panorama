import type { Request, Response } from "express";

// Resolves a short code to its destination. A link we cannot serve is reported
// with the standard error envelope so clients can switch on `error.code`.
export function handleResolve(req: Request, res: Response): void {
  const link = lookupTarget(req.params.id);
  if (!link) {
    res.status(410).json({ error: { code: "gone", message: "link no longer available" } });
    return;
  }
  res.json({ destination: link });
}

function lookupTarget(id: string): string | null {
  return id ? `https://example.com/${id}` : null;
}
