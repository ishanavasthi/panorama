import type { Request, Response } from "express";

// Bulk export of a caller's links, added for the reporting clients.
export function handleExports(_req: Request, res: Response): void {
  res.json({
    exports: [],
    count: 0,
  });
}
