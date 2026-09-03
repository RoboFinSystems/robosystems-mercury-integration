"""Transform — Mercury transactions into RoboLedger event payloads, plus the
chart of accounts and the counterparties those events refer to.

The shape follows the platform's adapter thesis: an integration emits
*events* ("Stripe paid us $95.14 on the 13th"), never GL rows. Every
event lands ``captured`` in the inbox with a Tier-0 suggestion attached
(``metadata.suggested_account``); posting is a classification the
operator (or Claude over MCP) makes once, and remembers.

Three event types come out of a bank feed:

- ``bank_transaction`` — money in or out against a third party (card
  spend, ACH, wires, payouts, interest, cashback).
- ``internal_transfer`` — one event per *pair* of legs between the org's
  own Mercury accounts (checking ↔ savings, the IO card autopay), so a
  movement never double-counts.
- ``external_transfer`` — a transfer to or from a bank account outside
  Mercury. Ownership is unknowable from the feed (owner draw? another
  entity? a loan?), so these carry no suggestion.

Fees Mercury bills directly are ``bank_fee``.

Accounts are **resolved, never created**: suggestions are matched against
the chart the graph already has (by qname, then by normalized name, then
by code) and stay a name-only hint when nothing matches. The integration
authors a chart only when run with ``--init-chart``.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from integration.mapping import AccountDef, Mapping, slug

# Failed/cancelled/blocked/reversed never become events. Pending ones are
# deferred: an event is written once, when the transaction posts, so the
# captured payload is the settled one (events cannot be re-shaped later).
SKIP_STATUSES = frozenset({"failed", "cancelled", "blocked", "reversed", "pending"})
TRANSFER_KINDS = frozenset({"internalTransfer", "treasuryTransfer"})
FEE_KINDS = frozenset(
  {
    "wireFee",
    "cardInternationalTransactionFee",
    "personalBankingSubscriptionFee",
    "billingEngineSubscriptionFee",
  }
)
GOVERNMENT_CATEGORIES = frozenset({"Taxes", "GovernmentServices"})
# The checking-account side of an IO card autopay names the card this way.
CREDIT_CARD_LABEL = "Mercury Credit"


@dataclass(frozen=True)
class BankAccount:
  mercury_id: str
  qname: str
  name: str
  kind: str
  trait: str
  balance_type: str
  code: str


@dataclass
class TransformResult:
  events: list[dict[str, Any]]
  skipped: Counter[str] = field(default_factory=Counter)
  classification: Counter[str] = field(default_factory=Counter)
  resolved: Counter[str] = field(default_factory=Counter)


def cents(amount: float | int | str) -> int:
  return round(float(amount) * 100)


# ── Element resolution ─────────────────────────────────────────────────


def name_key(value: str) -> str:
  """``Mercury Checking ••5424`` and ``Mercury Checking 5424`` resolve alike."""
  return "name:" + re.sub(r"[^a-z0-9]", "", value.lower())


def resolve_element(index: dict[str, str], *candidates: str | None) -> str | None:
  """First element id whose qname, normalized name, or code matches.

  ``index`` maps ``qname``, ``name:<normalized>`` and ``code:<code>`` to
  element ids (see ``setup.chart_element_index``). Candidates are tried in
  order; a bare string is tried as a qname, as a name, and as a code.
  """
  for candidate in candidates:
    if not candidate:
      continue
    for key in (candidate, name_key(candidate), f"code:{candidate}"):
      element_id = index.get(key)
      if element_id:
        return element_id
  return None


# ── Chart of accounts ──────────────────────────────────────────────────


def bank_accounts(raw: dict[str, Any]) -> list[BankAccount]:
  accounts: list[BankAccount] = []
  for index, acct in enumerate(raw.get("accounts") or [], start=1):
    kind = str(acct.get("kind") or acct.get("type") or "checking")
    name = str(acct.get("name") or f"Mercury {kind.title()}")
    accounts.append(
      BankAccount(
        mercury_id=acct["id"],
        qname=f"mercury:acct_{slug(name)}",
        name=name,
        kind=kind,
        trait="asset",
        balance_type="debit",
        code=f"10{index:02d}",
      )
    )
  for index, acct in enumerate(raw.get("credit_accounts") or [], start=1):
    name = str(
      acct.get("name")
      or ("Mercury Credit Card" if index == 1 else f"Mercury Credit Card {index}")
    )
    accounts.append(
      BankAccount(
        mercury_id=acct["id"],
        qname=f"mercury:acct_{slug(name)}",
        name=name,
        kind="credit",
        trait="liability",
        balance_type="credit",
        code=f"20{index:02d}",
      )
    )
  return accounts


def own_counterparty_names(accounts: list[BankAccount]) -> set[str]:
  names = {account.name for account in accounts}
  if any(account.kind == "credit" for account in accounts):
    names.add(CREDIT_CARD_LABEL)
  return names


def chart_elements(raw: dict[str, Any], mapping: Mapping) -> list[dict[str, Any]]:
  """Element requests for the starter chart (``--init-chart`` only).

  Bank and card accounts from the API, every account in ``mapping.toml``,
  and one account per GL code the feed has actually seen.
  """
  elements: list[dict[str, Any]] = []
  for account in bank_accounts(raw):
    elements.append(
      {
        "qname": account.qname,
        "name": account.name,
        "trait": account.trait,
        "balance_type": account.balance_type,
        "period_type": "instant",
        "code": account.code,
        "metadata": {
          "mercury_account_id": account.mercury_id,
          "mercury_kind": account.kind,
        },
      }
    )
  seen = {element["qname"] for element in elements}
  definitions: list[AccountDef] = list(mapping.accounts.values())
  for txn in raw.get("transactions") or []:
    for allocation in txn.get("glAllocations") or []:
      if allocation.get("glCodeName"):
        definitions.append(mapping.gl_account(allocation["glCodeName"]))
  for definition in definitions:
    if definition.qname in seen:
      continue
    seen.add(definition.qname)
    elements.append(
      {
        "qname": definition.qname,
        "name": definition.name,
        "trait": definition.trait,
        "balance_type": definition.balance_type,
        "period_type": "duration"
        if definition.trait in ("revenue", "expense")
        else "instant",
      }
    )
  return elements


# ── Counterparties ─────────────────────────────────────────────────────


def counterparty_key(txn: dict[str, Any]) -> str | None:
  name = txn.get("counterpartyName")
  if not name:
    return None
  return txn.get("counterpartyId") or f"name:{slug(name).lower()}"


def counterparties(
  raw: dict[str, Any], own_names: set[str], source_name: str
) -> list[dict[str, Any]]:
  """One agent per distinct third party the feed transacts with."""
  by_key: dict[str, dict[str, Any]] = {}
  for txn in raw.get("transactions") or []:
    if txn.get("status") in SKIP_STATUSES or txn.get("kind") in TRANSFER_KINDS:
      continue
    name = txn.get("counterpartyName")
    key = counterparty_key(txn)
    if not name or key is None or name in own_names:
      continue
    record = by_key.setdefault(
      key, {"name": name, "net": 0, "count": 0, "government": False}
    )
    record["net"] += cents(txn.get("amount") or 0)
    record["count"] += 1
    if txn.get("mercuryCategory") in GOVERNMENT_CATEGORIES:
      record["government"] = True
  agents: list[dict[str, Any]] = []
  for key, record in by_key.items():
    if record["government"]:
      agent_type = "government"
    elif str(record["name"]).startswith("Mercury"):
      agent_type = "other"
    else:
      agent_type = "customer" if record["net"] > 0 else "vendor"
    agents.append(
      {
        "agent_type": agent_type,
        "name": record["name"],
        "source": source_name,
        "external_id": key,
        "metadata": {"mercury_transactions": record["count"]},
      }
    )
  return agents


# ── Events ─────────────────────────────────────────────────────────────


def transform(
  raw: dict[str, Any],
  mapping: Mapping,
  *,
  source_name: str,
  element_ids: dict[str, str] | None = None,
  agent_ids: dict[str, str] | None = None,
) -> TransformResult:
  """``element_ids`` is the chart index from ``setup.chart_element_index``
  (qname, ``name:<normalized>`` and ``code:<code>`` keys); empty when the
  graph has no chart, in which case every suggestion stays a name hint."""
  element_ids = element_ids or {}
  agent_ids = agent_ids or {}
  accounts = {account.mercury_id: account for account in bank_accounts(raw)}
  own_names = own_counterparty_names(list(accounts.values()))
  result = TransformResult(events=[])

  live: list[dict[str, Any]] = []
  for txn in raw.get("transactions") or []:
    status = str(txn.get("status") or "unknown")
    if status in SKIP_STATUSES:
      result.skipped[status] += 1
      continue
    live.append(txn)

  # Pair the two legs of a movement between the org's own accounts.
  buckets: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
  for txn in live:
    if txn.get("kind") in TRANSFER_KINDS or txn.get("counterpartyName") in own_names:
      buckets[(_day(txn), abs(cents(txn.get("amount") or 0)))].append(txn)
  paired: set[str] = set()
  for group in buckets.values():
    debits = [t for t in group if cents(t.get("amount") or 0) < 0]
    credits = [t for t in group if cents(t.get("amount") or 0) > 0]
    for source_leg, target_leg in zip(debits, credits, strict=False):
      paired.update({source_leg["id"], target_leg["id"]})
      result.events.append(
        _transfer_event(source_leg, target_leg, accounts, element_ids, source_name)
      )
      result.classification["transfer"] += 1

  for txn in live:
    if txn["id"] in paired:
      continue
    event, classification = _bank_event(
      txn, accounts, own_names, mapping, element_ids, agent_ids, source_name
    )
    result.classification[classification] += 1
    if event["metadata"].get("suggested_account"):
      result.resolved[
        "resolved" if event["metadata"].get("suggested_element_id") else "hint_only"
      ] += 1
    result.events.append(event)

  result.events.sort(key=lambda event: str(event["occurred_at"]))
  return result


def _day(txn: dict[str, Any]) -> str:
  return str(txn.get("postedAt") or txn.get("createdAt") or "")[:10]


def _when(txn: dict[str, Any]) -> str:
  return str(txn.get("postedAt") or txn.get("createdAt"))


def _suggest(txn: dict[str, Any], mapping: Mapping) -> tuple[AccountDef | None, str]:
  allocations = txn.get("glAllocations") or []
  if allocations and allocations[0].get("glCodeName"):
    return mapping.gl_account(allocations[0]["glCodeName"]), "gl_allocation"
  custom = (txn.get("categoryData") or {}).get("name")
  if custom in mapping.custom_categories:
    qname = mapping.custom_categories[custom]
    return (mapping.accounts.get(qname) if qname else None), "custom_category"
  bucket = txn.get("mercuryCategory")
  if bucket in mapping.mercury_categories:
    qname = mapping.mercury_categories[bucket]
    return (mapping.accounts.get(qname) if qname else None), "mercury_category"
  return None, "none"


def _account_element(index: dict[str, str], account: BankAccount | None) -> str | None:
  if account is None:
    return None
  return resolve_element(index, account.qname, account.name)


def _transfer_event(
  source_leg: dict[str, Any],
  target_leg: dict[str, Any],
  accounts: dict[str, BankAccount],
  element_ids: dict[str, str],
  source_name: str,
) -> dict[str, Any]:
  from_account = accounts.get(str(source_leg.get("accountId")))
  to_account = accounts.get(str(target_leg.get("accountId")))
  from_name = from_account.name if from_account else "external"
  to_name = to_account.name if to_account else "external"
  metadata = _prune(
    {
      "kind": "internal_transfer",
      "status": target_leg.get("status"),
      "from_account_id": source_leg.get("accountId"),
      "to_account_id": target_leg.get("accountId"),
      "from_account_ref": from_account.qname if from_account else None,
      "to_account_ref": to_account.qname if to_account else None,
      "from_account_name": from_account.name if from_account else None,
      "to_account_name": to_account.name if to_account else None,
      "from_element_id": _account_element(element_ids, from_account),
      "to_element_id": _account_element(element_ids, to_account),
      "legs": [source_leg["id"], target_leg["id"]],
      "bank_description": target_leg.get("bankDescription")
      or source_leg.get("bankDescription"),
      "classification_source": "transfer",
      "dashboard_link": target_leg.get("dashboardLink"),
    }
  )
  return _prune(
    {
      "event_type": "internal_transfer",
      "event_category": "treasury",
      "event_class": "economic",
      "resource_type": "money",
      "occurred_at": _when(target_leg),
      "source": source_name,
      "external_id": f"mercury_xfer_{min(source_leg['id'], target_leg['id'])}",
      "external_url": target_leg.get("dashboardLink"),
      "amount": abs(cents(target_leg.get("amount") or 0)),
      "currency": "USD",
      "description": f"Transfer {from_name} to {to_name}",
      "resource_element_id": _account_element(element_ids, to_account),
      "metadata": metadata,
      "apply_handlers": False,
    }
  )


def _bank_event(
  txn: dict[str, Any],
  accounts: dict[str, BankAccount],
  own_names: set[str],
  mapping: Mapping,
  element_ids: dict[str, str],
  agent_ids: dict[str, str],
  source_name: str,
) -> tuple[dict[str, Any], str]:
  amount = cents(txn.get("amount") or 0)
  kind = str(txn.get("kind") or "other")
  name = str(txn.get("counterpartyName") or txn.get("bankDescription") or "Unknown")
  account = accounts.get(str(txn.get("accountId")))
  suggested, classification = _suggest(txn, mapping)

  if kind == "externalTransfer":
    event_type, category = "external_transfer", "treasury"
    suggested, classification = None, "transfer"
  elif kind in FEE_KINDS or (kind == "other" and txn.get("feeId")):
    event_type, category = "bank_fee", "treasury"
    suggested = suggested or mapping.accounts.get("mercury:BankFees")
  elif kind == "interestPayment":
    event_type, category = "bank_transaction", "treasury"
    suggested = suggested or mapping.accounts.get("mercury:InterestIncome")
  elif name in own_names or (name.startswith("Mercury") and amount > 0):
    event_type, category = "bank_transaction", "treasury"
  else:
    event_type = "bank_transaction"
    category = "sales" if amount > 0 else "purchase"

  agent_key = (
    None if name in own_names or kind in TRANSFER_KINDS else counterparty_key(txn)
  )
  allocations = txn.get("glAllocations") or []
  merchant = txn.get("merchant") or {}
  gl_code_name = allocations[0].get("glCodeName") if allocations else None
  suggested_element_id = (
    resolve_element(element_ids, suggested.qname, suggested.name, gl_code_name)
    if suggested
    else None
  )
  metadata = _prune(
    {
      "kind": kind,
      "status": txn.get("status"),
      "posted_at": txn.get("postedAt"),
      "created_at": txn.get("createdAt"),
      "account_id": txn.get("accountId"),
      "account_ref": account.qname if account else None,
      "account_name": account.name if account else None,
      "counterparty_id": txn.get("counterpartyId"),
      "counterparty_name": txn.get("counterpartyName"),
      "counterparty_external_id": agent_key,
      "bank_description": txn.get("bankDescription"),
      "external_memo": txn.get("externalMemo"),
      "note": txn.get("note"),
      "mercury_category": txn.get("mercuryCategory"),
      "custom_category": (txn.get("categoryData") or {}).get("name"),
      "gl_allocations": [
        {
          "gl_code_name": allocation.get("glCodeName"),
          "amount": allocation.get("amount"),
          "description": allocation.get("description"),
        }
        for allocation in allocations
      ]
      or None,
      "split": len(allocations) > 1 or None,
      "merchant_category": merchant.get("category"),
      "merchant_category_code": merchant.get("categoryCode"),
      "card_id": txn.get("cardId"),
      "suggested_account": suggested.qname if suggested else None,
      "suggested_account_name": suggested.name if suggested else None,
      "suggested_element_id": suggested_element_id,
      "classification_source": classification,
      "dashboard_link": txn.get("dashboardLink"),
    }
  )
  description = name
  if txn.get("bankDescription") and txn.get("bankDescription") != name:
    description = f"{name}: {txn['bankDescription']}"
  if kind in ("creditCardTransaction", "debitCardTransaction"):
    description = f"{name} (card)"
  event = {
    "event_type": event_type,
    "event_category": category,
    "event_class": "economic",
    "resource_type": "money",
    "occurred_at": _when(txn),
    "source": source_name,
    "external_id": f"mercury_txn_{txn['id']}",
    "external_url": txn.get("dashboardLink"),
    "amount": amount,
    "currency": "USD",
    "description": description[:200],
    "agent_id": agent_ids.get(agent_key) if agent_key else None,
    "resource_element_id": _account_element(element_ids, account),
    "metadata": metadata,
    "apply_handlers": False,
  }
  return _prune(event), classification


def _prune(payload: dict[str, Any]) -> dict[str, Any]:
  return {key: value for key, value in payload.items() if value is not None}
