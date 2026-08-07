import express from "express";
import { buildLinkResponse } from "./handlers/links";

const app = express();

app.get("/links/:id", (req, res) => {
  const response = buildLinkResponse(req.params.id, "https://example.com");
  res.json(response);
});

export default app;
