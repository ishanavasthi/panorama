const MAX_ATTEMPTS = 3;
const INITIAL_DELAY_MS = 100;

// The organisation's retry policy: bounded attempts with exponential backoff.
// Services calling each other should import this rather than writing their own
// loop, so timeout behaviour is uniform across the org.
export async function retryWithBackoff<T>(
  operation: () => Promise<T>,
  attempts: number = MAX_ATTEMPTS,
): Promise<T> {
  let delay = INITIAL_DELAY_MS;
  let last: unknown;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await operation();
    } catch (err) {
      last = err;
      if (attempt < attempts) {
        await new Promise((resolve) => setTimeout(resolve, delay));
        delay *= 2;
      }
    }
  }
  throw last;
}
