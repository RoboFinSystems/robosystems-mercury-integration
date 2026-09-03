"""Idempotent platform setup, run before every emit.

Three steps, each a lookup first so re-runs are no-ops:

1. **The source.** An ``external`` connection named after this
   integration (``INTEGRATION_SOURCE_NAME``, default ``mercury``) — the
   platform validates every event's ``source`` against its registered
   connections, and the row is what the connections page shows.
2. **The chart of accounts — read, not written.** Suggestions resolve
   against whatever chart the graph already has (a QuickBooks import, a
   template the operator initialized). Only ``--init-chart`` authors the
   starter chart, and it only ever adds accounts; nothing is removed.
3. **Counterparties.** One REA ``Agent`` per third party, keyed by
   Mercury's counterparty id.

Everything is written through the public SDK with the graph's API key.
The Mercury token never leaves this process.
"""

from __future__ import annotations

import json
from typing import Any

from robosystems_client.api.connections import create_connection as _create_connection
from robosystems_client.api.extensions_robo_ledger import create_agent as _create_agent
from robosystems_client.api.extensions_robo_ledger import (
  create_taxonomy_block as _create_taxonomy_block,
)
from robosystems_client.api.extensions_robo_ledger import (
  update_taxonomy_block as _update_taxonomy_block,
)
from robosystems_client.models import (
  CreateAgentRequest,
  CreateConnectionRequest,
  CreateConnectionRequestProvider,
  CreateTaxonomyBlockRequest,
  ExternalConnectionConfig,
  UpdateTaxonomyBlockRequest,
)

from integration.client import IntegrationAPIError, IntegrationClient

CHART_NAME = "Mercury Chart of Accounts"


def ensure_source(client: IntegrationClient) -> dict[str, Any]:
  graph_id = client.config.graph_id
  source_name = client.config.source_name
  http = client.sdk.get_httpx_client()
  response = http.get(f"/v1/graphs/{graph_id}/connections")
  response.raise_for_status()
  body = response.json()
  connections = body if isinstance(body, list) else body.get("connections", [])
  for connection in connections:
    if (
      connection.get("provider") == "external"
      and connection.get("source_name") == source_name
      and connection.get("status") != "disconnected"
    ):
      return connection
  created = _create_connection.sync_detailed(
    graph_id,
    client=client.sdk,
    body=CreateConnectionRequest(
      provider=CreateConnectionRequestProvider.EXTERNAL,
      external_config=ExternalConnectionConfig(
        source_name=source_name, display_name="Mercury"
      ),
    ),
  )
  if int(created.status_code) >= 400:
    raise IntegrationAPIError(
      f"register source {source_name!r} -> HTTP {created.status_code}: "
      f"{created.content[:500].decode(errors='replace')}"
    )
  return json.loads(created.content or b"{}")


def chart_element_index(client: IntegrationClient) -> dict[str, str]:
  """Read-only index of every chart-of-accounts element on the graph.

  Keys: the element's ``qname``, ``name:<normalized name>`` and
  ``code:<code>`` — the three ways a Tier-0 suggestion can resolve against
  a chart this integration did not author (a QuickBooks-imported chart
  carries the same account names Mercury's GL allocations use). Empty
  when the graph has no chart.
  """
  from integration.transform import name_key

  data = client.graphql("{ taxonomies { taxonomies { id taxonomyType } } }")
  taxonomies = (data.get("taxonomies") or {}).get("taxonomies") or []
  index: dict[str, str] = {}
  for taxonomy in taxonomies:
    if taxonomy.get("taxonomyType") != "chart_of_accounts":
      continue
    rows = _element_rows(client, taxonomy["id"])
    for row in rows:
      if row.get("qname"):
        index.setdefault(row["qname"], row["id"])
      if row.get("name"):
        index.setdefault(name_key(row["name"]), row["id"])
      if row.get("code"):
        index.setdefault(f"code:{row['code']}", row["id"])
  return index


def init_chart(
  client: IntegrationClient, elements: list[dict[str, Any]]
) -> dict[str, str]:
  """Create or extend the starter chart — **only** on ``--init-chart``.

  Never run by default: a graph that imported QuickBooks already has its
  chart, and the platform's rule is that no chart is created unless the
  operator asks for one. Returns ``{qname: element_id}``.
  """
  graph_id = client.config.graph_id
  chart = _find_chart(client)
  if chart is None:
    payload = {
      "name": CHART_NAME,
      "taxonomy_type": "chart_of_accounts",
      "description": (
        "Bank and card accounts from the Mercury API plus the accounts "
        "mapping.toml suggests. Authored by robosystems-mercury-integration."
      ),
      "elements": elements,
      "structures": [{"name": "main", "block_type": "chart_of_accounts"}],
    }
    client.unwrap(
      _create_taxonomy_block.sync_detailed(
        graph_id,
        client=client.sdk,
        body=CreateTaxonomyBlockRequest.from_dict(payload),
      )
    )
    chart = _find_chart(client)
    if chart is None:
      raise IntegrationAPIError("chart of accounts was created but cannot be found")
    return _element_ids(client, chart["id"])

  existing = _element_ids(client, chart["id"])
  missing = [element for element in elements if element["qname"] not in existing]
  if missing:
    client.unwrap(
      _update_taxonomy_block.sync_detailed(
        graph_id,
        client=client.sdk,
        body=UpdateTaxonomyBlockRequest.from_dict(
          {"taxonomy_id": chart["id"], "elements_to_add": missing}
        ),
      )
    )
    existing = _element_ids(client, chart["id"])
  return existing


def ensure_agents(
  client: IntegrationClient, agents: list[dict[str, Any]]
) -> dict[str, str]:
  """Create missing counterparties; return ``{external_id: agent_id}``."""
  existing = _agent_ids(client)
  created = 0
  for agent in agents:
    if agent["external_id"] in existing:
      continue
    client.unwrap(
      _create_agent.sync_detailed(
        client.config.graph_id,
        client=client.sdk,
        body=CreateAgentRequest.from_dict(agent),
        idempotency_key=f"{client.config.source_name}-agent-{agent['external_id']}",
      )
    )
    created += 1
  if created:
    existing = _agent_ids(client)
  return existing


def existing_event_external_ids(client: IntegrationClient) -> set[str]:
  """External ids of every event this source has already written, so a
  re-run sends only what is new and reports the rest as already there."""
  found: set[str] = set()
  page, offset = 500, 0
  while True:
    data = client.graphql(
      "query($source: String!, $limit: Int!, $offset: Int!) {"
      "  eventBlocks(source: $source, limit: $limit, offset: $offset) { externalId }"
      "}",
      {"source": client.config.source_name, "limit": page, "offset": offset},
    )
    rows = data.get("eventBlocks") or []
    found.update(row["externalId"] for row in rows if row.get("externalId"))
    if len(rows) < page:
      return found
    offset += page


# ── lookups ────────────────────────────────────────────────────────────


def _find_chart(client: IntegrationClient) -> dict[str, Any] | None:
  data = client.graphql("{ taxonomies { taxonomies { id name taxonomyType } } }")
  taxonomies = (data.get("taxonomies") or {}).get("taxonomies") or []
  charts = [t for t in taxonomies if t.get("taxonomyType") == "chart_of_accounts"]
  for chart in charts:
    if chart.get("name") == CHART_NAME:
      return chart
  return charts[0] if charts else None


def _element_rows(client: IntegrationClient, taxonomy_id: str) -> list[dict[str, Any]]:
  data = client.graphql(
    "query($taxonomyId: String!) {"
    "  elements(taxonomyId: $taxonomyId, limit: 1000) {"
    "    elements { id qname name code }"
    "  }"
    "}",
    {"taxonomyId": taxonomy_id},
  )
  return list((data.get("elements") or {}).get("elements") or [])


def _element_ids(client: IntegrationClient, taxonomy_id: str) -> dict[str, str]:
  rows = _element_rows(client, taxonomy_id)
  return {row["qname"]: row["id"] for row in rows if row.get("qname")}


def _agent_ids(client: IntegrationClient) -> dict[str, str]:
  data = client.graphql(
    "query($source: String!) {"
    "  agents(source: $source, limit: 500, isActive: null) { id externalId }"
    "}",
    {"source": client.config.source_name},
  )
  rows = data.get("agents") or []
  return {row["externalId"]: row["id"] for row in rows if row.get("externalId")}
