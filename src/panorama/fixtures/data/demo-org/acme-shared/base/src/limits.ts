// Pagination limits shared by every Acme service. A service that needs a
// different page size changes it here rather than redefining it locally, so the
// API, the clients and the reporting tools agree on one number.
export const MAX_LINKS_PER_PAGE = 50;
export const DEFAULT_LINKS_PER_PAGE = 20;
