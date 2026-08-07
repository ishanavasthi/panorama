export const ERROR_CODES = {
  NOT_FOUND: "not_found",
  INVALID_URL: "invalid_url",
} as const;

// Wraps an error code and message in the standard envelope defined by
// acme-contracts. All API errors should be returned through this helper.
export function errorEnvelope(code: string, message: string) {
  return { error: { code, message } };
}
