"""Extract — pull the Mercury feed and snapshot it.

Every run writes the raw pull to ``data/raw/<timestamp>/mercury.json``
(git-ignored). That snapshot is what makes a re-transform or a backfill
possible without re-pulling, and it is the audit copy of what the bank
said on the day.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from integration.config import Config
from integration.mercury import MercuryClient


def collect(config: Config) -> dict[str, Any]:
  client = MercuryClient(config.mercury_api_url, config.mercury_api_key)
  try:
    raw: dict[str, Any] = {
      "pulled_at": datetime.now(UTC).isoformat(timespec="seconds"),
      "since": config.since.isoformat(),
      "accounts": client.accounts(),
      "credit_accounts": client.credit_accounts(),
      "categories": client.categories(),
      "transactions": client.transactions(config.since),
    }
  finally:
    client.close()
  snapshot(config.data_dir, raw)
  return raw


def snapshot(data_dir: Path, raw: dict[str, Any]) -> Path:
  stamp = raw["pulled_at"].replace(":", "").replace("+0000", "Z")
  target = data_dir / "raw" / stamp / "mercury.json"
  target.parent.mkdir(parents=True, exist_ok=True)
  target.write_text(json.dumps(raw, indent=2, sort_keys=True))
  return target


def load_snapshot(path: Path) -> dict[str, Any]:
  """Re-run transform + emit from a saved pull (``--from-snapshot``)."""
  return json.loads(path.read_text())
