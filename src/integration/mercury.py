"""Mercury API client — the read side of the integration.

Authenticates with a personal API token. A **Read Only** token is all
this integration needs, and read-only tokens need no IP allowlist (a
read-write token would, which rules it out on hosted runners anyway).

Two Mercury behaviours worth knowing:

- Tokens unused for 45 days are deleted by Mercury. Run this on a
  schedule (the included workflow is daily) and the token stays alive.
- ``GET /transactions`` spans every account in the org — checking,
  savings, treasury and the IO credit card — so one pull is the whole
  feed. Card transactions carry ``mercuryCategory`` (a merchant
  bucket); every transaction may carry the customer's own custom
  category and, when Mercury is linked to QuickBooks/Xero/NetSuite,
  the GL codes assigned there (``glAllocations``).

Docs: https://docs.mercury.com/reference (append ``.md`` to any page
for the machine-readable version).
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

import httpx

PAGE_SIZE = 500


class MercuryClient:
  def __init__(self, api_url: str, api_key: str, *, timeout: float = 60.0) -> None:
    self._http = httpx.Client(
      base_url=api_url,
      headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
      timeout=timeout,
    )

  def accounts(self) -> list[dict[str, Any]]:
    """Checking, savings and treasury accounts."""
    return list(self._get("/accounts").get("accounts") or [])

  def credit_accounts(self) -> list[dict[str, Any]]:
    """IO credit-card accounts (empty when the org has no card)."""
    try:
      return list(self._get("/credit").get("accounts") or [])
    except httpx.HTTPStatusError as exc:
      if exc.response.status_code in (403, 404):
        return []
      raise

  def categories(self) -> list[dict[str, Any]]:
    """The org's custom transaction categories (Mercury seeds ~20)."""
    return list(self._get("/categories").get("categories") or [])

  def transactions(
    self, since: date, until: date | None = None
  ) -> list[dict[str, Any]]:
    """Every transaction across all accounts from ``since``, oldest first.

    Pages by ``start_after`` (the last id of the previous page) when a
    page comes back full.
    """
    params: dict[str, Any] = {
      "start": since.isoformat(),
      "limit": PAGE_SIZE,
      "order": "asc",
    }
    if until is not None:
      params["end"] = until.isoformat()
    collected: list[dict[str, Any]] = []
    while True:
      page = list(self._get("/transactions", params=params).get("transactions") or [])
      collected.extend(page)
      if len(page) < PAGE_SIZE:
        return collected
      params["start_after"] = page[-1]["id"]

  def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    attempts = 4
    for attempt in range(attempts):
      response = self._http.get(path, params=params)
      if response.status_code == 429 and attempt < attempts - 1:
        time.sleep(float(response.headers.get("Retry-After", 2**attempt)))
        continue
      response.raise_for_status()
      body = response.json()
      return body if isinstance(body, dict) else {}
    raise RuntimeError("unreachable")

  def close(self) -> None:
    self._http.close()
