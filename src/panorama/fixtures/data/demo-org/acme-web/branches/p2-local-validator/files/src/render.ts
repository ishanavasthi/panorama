import { fetchLink } from "./api/links";
import { validateUrl } from "./util/validateUrl";

export async function renderLink(id: string): Promise<string> {
  const { url } = await fetchLink(id);
  if (!validateUrl(url)) {
    return "<span>invalid link</span>";
  }
  return `<a href="${url}">${url}</a>`;
}
