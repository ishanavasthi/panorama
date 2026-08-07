// UTC ISO-8601 timestamp formatting, per the acme-contracts convention.
export function formatTimestamp(date: Date): string {
  return date.toISOString();
}
