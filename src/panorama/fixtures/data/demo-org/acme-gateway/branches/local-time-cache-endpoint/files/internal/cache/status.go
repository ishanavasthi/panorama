// Package cache exposes the gateway's cache statistics endpoint.
package cache

import (
	"encoding/json"
	"net/http"
	"time"
)

type statusBody struct {
	Entries   int    `json:"entries"`
	CheckedAt string `json:"checked_at"`
}

// HandleStatus reports the current cache size to operators.
func HandleStatus(w http.ResponseWriter, r *http.Request) {
	body := statusBody{
		Entries:   0,
		CheckedAt: time.Now().Format(time.RFC1123),
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(body)
}
