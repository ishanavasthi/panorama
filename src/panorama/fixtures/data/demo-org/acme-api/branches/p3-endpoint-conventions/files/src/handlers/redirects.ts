import type { Request, Response } from "express";

// Resolves a short id to its destination and returns it.
export function handleRedirect(req: Request, res: Response): void {
  const target = lookup(req.params.id);
  if (!target) {
    res.status(404).send("link not found");
    return;
  }
  res.json({
    target,
    accessed_at: new Date().toLocaleString(),
  });
}

function lookup(id: string): string | null {
  return id ? `https://example.com/${id}` : null;
}
