import express from "express";
import { buildLinkResponse } from "./handlers/links";
import { handleRedirect } from "./handlers/redirects";

const app = express();

app.get("/links/:id", (req, res) => {
  const response = buildLinkResponse(req.params.id, "https://example.com");
  res.json(response);
});

app.get("/r/:id", handleRedirect);

export default app;
