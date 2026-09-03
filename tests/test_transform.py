from __future__ import annotations

from pathlib import Path

import pytest

from integration.mapping import infer_trait, load_mapping, slug
from integration.transform import (
  bank_accounts,
  chart_elements,
  counterparties,
  own_counterparty_names,
  transform,
)

MAPPING = load_mapping(Path(__file__).resolve().parents[1] / "mapping.toml")
CHECKING = "acct-checking"
CREDIT = "acct-credit"


def txn(**overrides):
  base = {
    "id": overrides.pop("id"),
    "accountId": CHECKING,
    "amount": 0.0,
    "status": "sent",
    "kind": "other",
    "createdAt": "2026-08-13T12:00:00.000Z",
    "postedAt": "2026-08-13T12:00:00.000Z",
    "counterpartyId": None,
    "counterpartyName": None,
    "bankDescription": None,
    "externalMemo": None,
    "note": None,
    "mercuryCategory": None,
    "categoryData": None,
    "glAllocations": [],
    "merchant": None,
    "cardId": None,
    "feeId": None,
    "dashboardLink": "https://app.mercury.com/transactions/x",
  }
  base.update(overrides)
  return base


@pytest.fixture
def raw():
  return {
    "pulled_at": "2026-09-03T00:00:00+00:00",
    "since": "2026-02-01",
    "accounts": [
      {"id": CHECKING, "kind": "checking", "name": "Mercury Checking ••5424"},
      {"id": "acct-savings", "kind": "savings", "name": "Mercury Savings ••4881"},
    ],
    "credit_accounts": [{"id": CREDIT, "status": "active"}],
    "categories": [],
    "transactions": [
      txn(
        id="card-1",
        accountId=CREDIT,
        amount=-28.2,
        kind="creditCardTransaction",
        counterpartyId="cp-qb",
        counterpartyName="QuickBooks",
        mercuryCategory="Software",
        categoryData={"name": "Software & Subscriptions"},
        glAllocations=[
          {"glCodeName": "General & Administrative:Technology", "amount": -28.2}
        ],
        merchant={"category": "Software", "categoryCode": "5734"},
      ),
      txn(
        id="stripe-1",
        amount=95.14,
        counterpartyId="cp-stripe",
        counterpartyName="STRIPE",
        bankDescription="STRIPE; TRANSFER; RFS LLC",
        categoryData={"name": "Revenue"},
        glAllocations=[{"glCodeName": "Subscription Revenue", "amount": 95.14}],
      ),
      txn(
        id="autopay-checking",
        amount=-28.2,
        counterpartyName="Mercury Credit",
        bankDescription="IO AUTOPAY",
        postedAt="2026-08-04T09:00:00.000Z",
      ),
      txn(
        id="autopay-credit",
        accountId=CREDIT,
        amount=28.2,
        counterpartyName="Mercury Checking ••5424",
        bankDescription="IO AUTOPAY",
        postedAt="2026-08-04T09:00:00.000Z",
      ),
      txn(
        id="cashback-1",
        accountId=CREDIT,
        amount=0.42,
        counterpartyName="Mercury IO Cashback",
        glAllocations=[{"glCodeName": "Credit card rewards", "amount": 0.42}],
      ),
      txn(
        id="failed-1",
        accountId=CREDIT,
        amount=0.0,
        status="failed",
        kind="creditCardTransaction",
        counterpartyName="MATCH HOSPITALITY",
      ),
      txn(
        id="pending-1",
        accountId=CREDIT,
        amount=-40.28,
        status="pending",
        kind="creditCardTransaction",
        counterpartyName="Anthropic",
        postedAt=None,
      ),
      txn(
        id="chase-1",
        amount=100.0,
        kind="externalTransfer",
        counterpartyName="Chase - Checking ••3253",
        categoryData={"name": "Transfer"},
      ),
      txn(
        id="wyoming-1",
        accountId=CREDIT,
        amount=-62.0,
        kind="creditCardTransaction",
        counterpartyName="Wyoming Secretary of State's Office",
        mercuryCategory="GovernmentServices",
      ),
    ],
  }


def test_bank_accounts_and_own_names(raw):
  accounts = bank_accounts(raw)
  assert [a.code for a in accounts] == ["1001", "1002", "2001"]
  assert accounts[2].name == "Mercury Credit Card"
  assert accounts[2].trait == "liability"
  assert "Mercury Credit" in own_counterparty_names(accounts)


def test_chart_includes_banks_mapping_and_seen_gl_codes(raw):
  elements = chart_elements(raw, MAPPING)
  qnames = {e["qname"] for e in elements}
  assert "mercury:acct_MercuryChecking5424" in qnames
  assert "mercury:SoftwareSubscriptions" in qnames
  assert "mercury:gl_GeneralAdministrativeTechnology" in qnames
  revenue = next(e for e in elements if e["qname"] == "mercury:gl_SubscriptionRevenue")
  assert revenue["trait"] == "revenue" and revenue["period_type"] == "duration"
  bank = next(e for e in elements if e["qname"] == "mercury:acct_MercuryChecking5424")
  assert bank["period_type"] == "instant"


def test_counterparties_exclude_own_accounts(raw):
  agents = counterparties(raw, own_counterparty_names(bank_accounts(raw)), "mercury")
  by_name = {a["name"]: a for a in agents}
  assert "Mercury Checking ••5424" not in by_name and "Mercury Credit" not in by_name
  assert by_name["QuickBooks"]["agent_type"] == "vendor"
  assert by_name["STRIPE"]["agent_type"] == "customer"
  assert by_name["Wyoming Secretary of State's Office"]["agent_type"] == "government"
  assert by_name["Mercury IO Cashback"]["agent_type"] == "other"
  assert by_name["STRIPE"]["external_id"] == "cp-stripe"
  assert by_name["Mercury IO Cashback"]["external_id"].startswith("name:")


def test_transform_pairs_transfers_and_classifies(raw):
  result = transform(
    raw,
    MAPPING,
    source_name="mercury",
    element_ids={
      # a QuickBooks-imported chart: names, not mercury:* qnames
      "name:mercurycreditcard": "elem_cc",
      "name:mercurychecking5424": "elem_chk",
      "name:generaladministrativetechnology": "elem_tech",
      "code:4000": "elem_rev",
    },
    agent_ids={"cp-stripe": "agt_stripe"},
  )
  by_id = {e["external_id"]: e for e in result.events}
  assert result.skipped == {"failed": 1, "pending": 1}
  assert len(result.events) == 6
  assert "mercury_txn_pending-1" not in by_id

  transfer = by_id["mercury_xfer_autopay-checking"]
  assert transfer["event_type"] == "internal_transfer"
  assert transfer["amount"] == 2820
  assert transfer["metadata"]["from_account_ref"] == "mercury:acct_MercuryChecking5424"
  assert transfer["metadata"]["to_element_id"] == "elem_cc"
  assert transfer["metadata"]["from_element_id"] == "elem_chk"
  assert "mercury_txn_autopay-credit" not in by_id

  card = by_id["mercury_txn_card-1"]
  assert card["event_category"] == "purchase" and card["amount"] == -2820
  assert card["metadata"]["classification_source"] == "gl_allocation"
  assert (
    card["metadata"]["suggested_account"]
    == "mercury:gl_GeneralAdministrativeTechnology"
  )
  assert card["resource_element_id"] == "elem_cc"
  assert card["metadata"]["suggested_element_id"] == "elem_tech"  # by GL code name
  assert card["apply_handlers"] is False

  stripe = by_id["mercury_txn_stripe-1"]
  assert stripe["event_category"] == "sales" and stripe["amount"] == 9514
  assert stripe["agent_id"] == "agt_stripe"
  assert stripe["metadata"]["suggested_account"] == "mercury:gl_SubscriptionRevenue"
  assert "suggested_element_id" not in stripe["metadata"]  # no such account: hint only
  assert result.resolved == {"resolved": 1, "hint_only": 3}

  cashback = by_id["mercury_txn_cashback-1"]
  assert cashback["event_category"] == "treasury"
  assert cashback["metadata"]["suggested_account"] == "mercury:CreditCardRewards"

  chase = by_id["mercury_txn_chase-1"]
  assert chase["event_type"] == "external_transfer"
  assert "suggested_account" not in chase["metadata"]

  wyoming = by_id["mercury_txn_wyoming-1"]
  assert wyoming["metadata"]["classification_source"] == "mercury_category"
  assert wyoming["metadata"]["suggested_account"] == "mercury:TaxesLicenses"

  assert [e["occurred_at"] for e in result.events] == sorted(
    e["occurred_at"] for e in result.events
  )


def test_infer_trait_and_slug():
  assert infer_trait("Subscription Revenue") == ("revenue", "credit")
  assert infer_trait("Accounts Payable") == ("liability", "credit")
  assert infer_trait("Prepaid expenses") == ("asset", "debit")
  assert infer_trait("General & Administrative:Technology") == ("expense", "debit")
  assert (
    slug("General & Administrative:Technology") == "GeneralAdministrativeTechnology"
  )
  assert slug("Mercury Checking ••5424") == "MercuryChecking5424"
