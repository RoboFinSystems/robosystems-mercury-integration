"""Lane 1 — ledger events via the SDK's ``create-event-block``.

The platform enforces the accounting discipline — the closed-period
gate, capture-then-approve, ``(source, external_id)`` idempotency — so
this emitter just delivers well-formed events, one call per event, and
reports what happened to each.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from robosystems_client.api.extensions_robo_ledger import (
  create_event_block as _create_event_block,
)
from robosystems_client.models import CreateEventBlockRequest
from robosystems_client.types import UNSET

from integration.client import IntegrationAPIError, IntegrationClient


@dataclass
class EmitReport:
  created: int = 0
  existing: int = 0
  failed: int = 0
  errors: list[str] = field(default_factory=list)


def emit_event(
  client: IntegrationClient,
  payload: dict[str, Any],
  *,
  idempotency_key: str | None = None,
) -> dict:
  """Create one event. ``source`` is stamped from config if absent;
  ``external_id`` is required — ``(source, external_id)`` is the natural
  key the platform deduplicates on, so no ``Idempotency-Key`` is sent by
  default (a key bound to a payload that later changes would 409)."""
  payload = {"source": client.config.source_name, **payload}
  if not payload.get("external_id"):
    raise ValueError(
      "external_id is required — a stable source-system id is what makes "
      "re-sends idempotent"
    )
  response = _create_event_block.sync_detailed(
    client.config.graph_id,
    client=client.sdk,
    body=CreateEventBlockRequest.from_dict(payload),
    idempotency_key=idempotency_key if idempotency_key is not None else UNSET,
  )
  return client.unwrap(response)


def emit_events(
  client: IntegrationClient, payloads: list[dict[str, Any]]
) -> EmitReport:
  """Create a batch, tolerating per-event failures so one bad row never
  blocks the feed. A duplicate ``(source, external_id)`` counts as
  ``existing``."""
  report = EmitReport()
  for payload in payloads:
    try:
      emit_event(client, payload)
      report.created += 1
    except IntegrationAPIError as exc:
      message = str(exc)
      if (
        "409" in message
        or "already exists" in message
        or "duplicate" in message.lower()
      ):
        report.existing += 1
        continue
      report.failed += 1
      if len(report.errors) < 10:
        report.errors.append(f"{payload.get('external_id')}: {message[:300]}")
  return report
