package shortcode

import "testing"

func TestParseShortCode(t *testing.T) {
	cases := []struct {
		input    string
		expected string
	}{
		{"/AbC123", "abc123"},
		{"/abc123/details", "abc123"},
		{"", ""},
	}
	for _, tc := range cases {
		if actual := parseShortCode(tc.input); actual != tc.expected {
			t.Errorf("parseShortCode(%q) = %q, expected %q", tc.input, actual, tc.expected)
		}
	}
}
