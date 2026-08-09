import express from "express";
import { buildLinkResponse } from "./handlers/links";
import { handleResolve } from "./handlers/resolve";
import { handleStats } from "./handlers/stats";

const app = express();

app.get("/links/:id", (req, res) => {
  const response = buildLinkResponse(req.params.id, "https://example.com");
  res.json(response);
});

app.get("/resolve/:id", handleResolve);
app.get("/stats", handleStats);

export default app;
