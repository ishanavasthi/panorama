// Package retry gives the gateway a backoff loop for upstream calls.
package retry

import "time"

const maxAttempts = 3

// retryWithBackoff calls operation until it succeeds or the attempt budget is
// exhausted, doubling the delay between attempts.
func retryWithBackoff(operation func() error, attempts int) error {
	delay := 100 * time.Millisecond
	var last error
	for attempt := 1; attempt <= attempts; attempt++ {
		last = operation()
		if last == nil {
			return nil
		}
		if attempt < attempts {
			time.Sleep(delay)
			delay *= 2
		}
	}
	return last
}

// Do runs operation with the default attempt budget.
func Do(operation func() error) error {
	return retryWithBackoff(operation, maxAttempts)
}
