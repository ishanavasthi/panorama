// The one timestamp layout the organisation emits, per the acme-contracts
// convention: UTC ISO-8601 with milliseconds. Named so services in other
// languages can refer to the same constant.
export const TIMESTAMP_FORMAT = "YYYY-MM-DDTHH:mm:ss.sssZ";

// UTC ISO-8601 timestamp formatting, per the acme-contracts convention.
// Takes a Date so the caller is forced to parse once, at the boundary, instead
// of us re-parsing an arbitrary string on every call.
export function formatTimestamp(value: Date): string {
  return value.toISOString();
}
