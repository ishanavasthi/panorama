import type { LinkView } from "./api/links";

const linkCache: Record<string, LinkView> = {};

export function rememberLink(view: LinkView): void {
  linkCache[view.id] = view;
}

// Returns the cached view for a short code.
export function cachedLink(id: string): LinkView {
  return linkCache[id];
}
