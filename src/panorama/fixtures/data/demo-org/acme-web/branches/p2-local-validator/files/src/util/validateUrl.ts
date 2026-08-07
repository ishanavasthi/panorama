// Local URL check used before rendering a link.
export function validateUrl(candidate: string): boolean {
  return candidate.startsWith("http://") || candidate.startsWith("https://");
}
