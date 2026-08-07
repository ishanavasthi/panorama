import type { LinkResponse } from "../types";

// Builds the JSON body returned by GET /links/:id. The id is the caller's
// short code; url is the resolved destination.
export function buildLinkResponse(id: string, url: string): LinkResponse {
  return {
    id,
    url,
    created_at: new Date().toISOString(),
  };
}
