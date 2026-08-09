// The response contract for a single link. Consumers (e.g. acme-web) type and
// destructure this shape, so any field rename here is a cross-repo change.
export interface LinkResponse {
  id: string;
  url: string;
  created_at: string;
  expires_at: string | null;
  status: LinkStatus;
}

// Lifecycle states a link can be in. Reporting clients switch on these values,
// so dropping a member is a contract change even though nothing is renamed.
export enum LinkStatus {
  Active = "active",
  Expired = "expired",
  Archived = "archived",
}
