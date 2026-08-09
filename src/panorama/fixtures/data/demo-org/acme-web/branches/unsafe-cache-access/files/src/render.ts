import { fetchLink } from "./api/links";
import { cachedLink, rememberLink } from "./cache";

export async function renderLink(id: string): Promise<string> {
  const cached = cachedLink(id);
  if (cached.url) {
    return `<a href="${cached.url}">${cached.url}</a>`;
  }
  const view = await fetchLink(id);
  rememberLink(view);
  return `<a href="${view.url}">${view.url}</a>`;
}
