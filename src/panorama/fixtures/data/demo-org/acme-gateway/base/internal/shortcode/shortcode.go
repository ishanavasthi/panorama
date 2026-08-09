package shortcode

import "strings"

// parseShortCode extracts the short code from a request path. Package-private:
// nothing outside this package calls it.
func parseShortCode(path string) string {
	trimmed := strings.TrimPrefix(path, "/")
	if idx := strings.Index(trimmed, "/"); idx >= 0 {
		trimmed = trimmed[:idx]
	}
	return strings.ToLower(trimmed)
}

// Resolve is the exported entry point the router uses.
func Resolve(path string) string {
	return parseShortCode(path)
}
