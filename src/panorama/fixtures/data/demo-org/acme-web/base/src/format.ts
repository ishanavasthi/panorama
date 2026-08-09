import { formatTimestamp } from "acme-shared";

// Renders a link's creation time for display. The API sends it as an
// already-serialised ISO string, which is what we hand to the shared formatter.
export function displayCreatedAt(createdAt: string): string {
  return formatTimestamp(createdAt);
}
