// The one timestamp layout the organisation emits, per the acme-contracts
// convention: UTC ISO-8601 with milliseconds. Named so services in other
// languages can refer to the same constant.
export const TIMESTAMP_FORMAT = "YYYY-MM-DDTHH:mm:ss.sssZ";

// UTC ISO-8601 timestamp formatting, per the acme-contracts convention.
// Accepts either a Date or an already-serialised timestamp string, because
// callers routinely hand us a value straight out of an API response.
export function formatTimestamp(value: Date | string): string {
  return new Date(value).toISOString();
}
