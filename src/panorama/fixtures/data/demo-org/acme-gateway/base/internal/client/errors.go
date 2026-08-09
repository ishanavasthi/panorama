package client

// envelope mirrors the error body the link API returns, as documented by the
// organisation's conventions repository.
type envelope struct {
	Error struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

// IsMissing reports whether an error envelope describes a link the API could
// not serve, by matching the documented error code rather than the status line.
func IsMissing(body envelope) bool {
	return body.Error.Code == codeNotFound
}
