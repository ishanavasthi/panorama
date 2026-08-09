package client

import (
	"encoding/json"
	"errors"
	"net/http"
	"time"
)

// ErrLinkNotFound is returned when the link API says a short code has no
// destination. The gateway maps it onto its own 404.
var ErrLinkNotFound = errors.New("link is not available")

// codeNotFound is the error code the link API puts in its error envelope. Any
// other code is treated as an upstream fault rather than a missing link.
const codeNotFound = "not_found"

// Link is the gateway's view of the link API response. Only the fields the
// gateway actually forwards are decoded.
type Link struct {
	ID        string  `json:"id"`
	ExpiresAt *string `json:"expires_at"`
}

// Fetch retrieves a single link from the link API.
func Fetch(base string, shortCode string) (*Link, error) {
	resp, err := http.Get(base + "/links/" + shortCode)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return nil, ErrLinkNotFound
	}

	var link Link
	if err := json.NewDecoder(resp.Body).Decode(&link); err != nil {
		return nil, err
	}
	return &link, nil
}

// Expired reports whether a link has passed its expiry. A nil expires_at means
// the link never expires, which is why the field decodes as a pointer.
func Expired(link *Link, now time.Time) bool {
	if link.ExpiresAt == nil {
		return false
	}
	deadline, err := time.Parse(time.RFC3339, *link.ExpiresAt)
	if err != nil {
		return false
	}
	return now.After(deadline)
}
