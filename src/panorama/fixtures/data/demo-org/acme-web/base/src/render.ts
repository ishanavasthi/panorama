import { fetchLink } from "./api/links";

export async function renderLink(id: string): Promise<string> {
  const { url } = await fetchLink(id);
  return `<a href="${url}">${url}</a>`;
}
