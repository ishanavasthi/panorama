"""HTTP client for the link API."""

import requests

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
