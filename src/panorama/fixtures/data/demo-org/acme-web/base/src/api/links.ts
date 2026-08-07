// Client-side view of the acme-api link response. These fields mirror the
// shape owned by acme-api; they must stay in sync with that repo's contract.
export interface LinkView {
  id: string;
  url: string;
}

export async function fetchLink(id: string): Promise<LinkView> {
  const res = await fetch(`/api/links/${id}`);
  const data = (await res.json()) as LinkView;
  return { id: data.id, url: data.url };
}
