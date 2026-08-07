import type { LinkResponse } from "../types";

// Builds the JSON body returned by GET /links/:id.
export function buildLinkResponse(id: string, url: string): LinkResponse {
  return {
    id,
    target_url: url,
    created_at: new Date().toISOString(),
  };
}
