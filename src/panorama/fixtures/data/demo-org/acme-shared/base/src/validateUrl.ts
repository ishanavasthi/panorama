const ALLOWED_PROTOCOLS = ["http:", "https:"];

// Canonical URL validation for the organisation. Consumers should import this
// rather than writing their own check.
export function validateUrl(candidate: string): boolean {
  try {
    const parsed = new URL(candidate);
    return ALLOWED_PROTOCOLS.includes(parsed.protocol);
  } catch {
    return false;
  }
}
