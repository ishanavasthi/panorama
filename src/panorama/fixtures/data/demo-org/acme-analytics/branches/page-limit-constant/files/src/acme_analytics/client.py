"""HTTP client for the link API."""

import requests

from .limits import DEFAULT_LINKS_PER_PAGE, MAX_LINKS_PER_PAGE

API_BASE = "https://api.acme.example"
HTTP_TIMEOUT = 10


def fetch_link(link_id):
    """Return one link record from GET /links/{id}."""
    response = requests.get(f"{API_BASE}/links/{link_id}", timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    return {
        "id": payload["id"],
        "created_at": payload["created_at"],
        "status": payload["status"],
    }


def fetch_link_page(page, per_page=DEFAULT_LINKS_PER_PAGE):
    """Return one page of link records, capped at the reporting page limit."""
    size = min(per_page, MAX_LINKS_PER_PAGE)
    response = requests.get(
        f"{API_BASE}/links",
        params={"page": page, "per_page": size},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def fetch_stats():
    """Return aggregate counts from GET /stats."""
    response = requests.get(f"{API_BASE}/stats", timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    return {
        "total_links": payload["total_links"],
        "active_links": payload["active_links"],
        "generated_at": payload["generated_at"],
    }
