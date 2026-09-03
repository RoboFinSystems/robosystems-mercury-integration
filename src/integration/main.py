"""Entry point: setup → collect → transform → emit.

Run locally with ``uv run python -m integration.main``. Flags:

  --dry-run            pull + transform, write data/preview/events.json,
                       touch nothing on the platform
  --init-chart         author the starter chart on a graph that has none
                       (explicit; never implied by a normal run)
  --from-snapshot P    transform + emit from a saved raw pull instead of
                       calling Mercury
  --limit N            emit only the first N events (smoke test)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from integration.client import IntegrationClient
from integration.collect import collect, load_snapshot
from integration.config import load_config
from integration.emit.events import emit_events
from integration.mapping import load_mapping
from integration.setup import (
  chart_element_index,
  ensure_agents,
  ensure_source,
  existing_event_external_ids,
  init_chart,
)
from integration.transform import (
  bank_accounts,
  chart_elements,
  counterparties,
  own_counterparty_names,
  transform,
)


def run(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(prog="integration")
  parser.add_argument("--dry-run", action="store_true")
  parser.add_argument("--from-snapshot", type=Path, default=None)
  parser.add_argument("--limit", type=int, default=None)
  parser.add_argument(
    "--init-chart",
    action="store_true",
    help=(
      "author the starter chart of accounts (bank/card accounts + mapping.toml "
      "accounts) on a graph that has none; never implied"
    ),
  )
  args = parser.parse_args(argv)

  config = load_config()
  mapping = load_mapping()
  raw = load_snapshot(args.from_snapshot) if args.from_snapshot else collect(config)
  accounts = bank_accounts(raw)
  print(
    f"mercury: {len(accounts)} accounts, {len(raw['transactions'])} transactions "
    f"since {raw['since']} (pulled {raw['pulled_at']})"
  )

  elements = chart_elements(raw, mapping)
  agents = counterparties(raw, own_counterparty_names(accounts), config.source_name)

  if args.dry_run:
    result = transform(raw, mapping, source_name=config.source_name)
    preview = config.data_dir / "preview" / "events.json"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_text(json.dumps(result.events, indent=2))
    print(f"chart: {len(elements)} accounts · counterparties: {len(agents)}")
    print(
      f"events: {len(result.events)} · skipped {dict(result.skipped)} · "
      f"classified {dict(result.classification)}"
    )
    print(f"preview written to {preview} (nothing sent)")
    return 0

  client = IntegrationClient(config)
  try:
    connection = ensure_source(client)
    print(f"source: {config.source_name} ({connection.get('status')})")
    if args.init_chart:
      init_chart(client, elements)
    element_ids = chart_element_index(client)
    if element_ids:
      print(f"chart: {len({v for v in element_ids.values()})} accounts on the graph")
    else:
      print(
        "chart: none on the graph — suggestions stay name-only hints "
        "(run with --init-chart to author the starter chart)"
      )
    agent_ids = ensure_agents(client, agents)
    print(f"counterparties: {len(agent_ids)}")

    result = transform(
      raw,
      mapping,
      source_name=config.source_name,
      element_ids=element_ids,
      agent_ids=agent_ids,
    )
    already = existing_event_external_ids(client)
    payloads = [e for e in result.events if e["external_id"] not in already]
    if args.limit:
      payloads = payloads[: args.limit]
    report = emit_events(client, payloads)
    report.existing += len(result.events) - len(payloads)
    print(
      f"events: {report.created} created · {report.existing} already there · "
      f"{report.failed} failed · skipped {dict(result.skipped)} · "
      f"classified {dict(result.classification)} · "
      f"suggestions {dict(result.resolved)}"
    )
    for error in report.errors:
      print(f"  ! {error}")
    return 1 if report.failed else 0
  finally:
    client.close()


if __name__ == "__main__":
  raise SystemExit(run())
