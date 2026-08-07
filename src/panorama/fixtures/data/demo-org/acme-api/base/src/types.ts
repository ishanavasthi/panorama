// The response contract for a single link. Consumers (e.g. acme-web) type and
// destructure this shape, so any field rename here is a cross-repo change.
export interface LinkResponse {
  id: string;
  url: string;
  created_at: string;
}
