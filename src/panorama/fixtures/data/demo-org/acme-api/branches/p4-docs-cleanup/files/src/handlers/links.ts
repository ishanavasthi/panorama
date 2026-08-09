import { LinkStatus, type LinkResponse } from "../types";

// Builds the JSON body returned by GET /links/:id. The id is the caller's
// short code; url is the resolved destination.
export function buildLinkResponse(id: string, url: string): LinkResponse {
  return {
    id: normalizeShortCode(id),
    url,
    created_at: new Date().toISOString(),
    expires_at: null,
    status: LinkStatus.Active,
  };
}

// Internal only: tidies a caller-supplied short code. Nothing outside this
// module uses it, and nothing outside this repository can.
function normalizeShortCode(value: string): string {
  return value.trim().toLowerCase();
}
