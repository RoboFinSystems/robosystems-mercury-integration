# RoboSystems Mercury Integration

Your Mercury bank feed, into your RoboLedger graph, with your own read-only
Mercury API token. Every transaction across checking, savings, treasury and
the IO card lands in the ledger inbox as a captured event with an account
suggestion attached. Nothing posts until you (or Claude) classify it.

Built from [robosystems-integration-template](https://github.com/RoboFinSystems/robosystems-integration-template)
and shaped the way that template intends: **you run it, with your credentials,
in your runtime**. The platform never sees the Mercury token, which is what
lets this work today without a Mercury OAuth partnership and keeps Mercury's
personal-token terms intact — it is your automation on your own account.

## What it does

Each run is `setup → collect → transform → emit`:

1. **Setup** (idempotent) registers `mercury` as a source on your graph and
   creates one counterparty (`Agent`) per third party in the feed. It
   **reads** your chart of accounts; it does not create one (see below).
2. **Collect** pulls accounts, the credit account, custom categories and every
   transaction since `MERCURY_SINCE`, and snapshots the raw pull to
   `data/raw/<timestamp>/mercury.json` (git-ignored — your audit copy).
3. **Transform** turns transactions into events:
   - `bank_transaction` — card spend, ACH, wires, payouts, interest, cashback.
   - `internal_transfer` — **one** event per pair of legs between your own
     Mercury accounts (checking ↔ savings, the IO card autopay), so a movement
     never double-counts.
   - `external_transfer` — to or from a bank outside Mercury. The feed cannot
     tell an owner draw from a loan or an intercompany move, so no suggestion.
   - `bank_fee` — fees Mercury bills directly.

   Failed, cancelled, blocked and reversed transactions are skipped. Pending
   ones are deferred until they post, so every event carries the settled
   amount and date.
4. **Emit** sends each new event through `create-event-block` with
   `apply_handlers=false`. Events the graph already holds for this source are
   skipped and reported as "already there", so a re-run, a backfill or a wider
   `MERCURY_SINCE` only ever adds.

### Suggestions, not decisions

Every event carries `metadata.suggested_account` and
`metadata.classification_source`, from three Mercury signals in order of
trust — see [`mapping.toml`](mapping.toml):

| Precedence | Signal | Where it comes from |
|---|---|---|
| 1 | `glAllocations` | the GL code assigned in Mercury's Accounting tab (yours or Mercury's auto-categorization) when Mercury is linked to QuickBooks / Xero / NetSuite |
| 2 | custom category | Mercury's per-transaction category (`categoryData`) |
| 3 | `mercuryCategory` | the merchant bucket Mercury stamps on card spend |

A suggestion is **resolved against the chart your graph already has** — by
qname, then by account name (so a QuickBooks-imported chart matches the names
Mercury's GL allocations carry), then by code — and lands as
`metadata.suggested_element_id`. When nothing matches, the suggestion stays a
name-only hint. The event's `resource_element_id` is the bank or card account
it hit, resolved the same way.

**Classification is a decision made on the platform, not here.** Review the
inbox, or let Claude do it over MCP: list the captured Mercury events, recall
what was decided for this counterparty before, patch the event with the
chosen account, and remember the decision. The mapping file only shapes the
hint; it never posts anything.

### The chart of accounts is yours, never ours

This integration does not create a chart of accounts. If your graph imported
QuickBooks, QuickBooks is the chart. If you started from a platform template,
that is the chart. A graph with no chart gets name-only hints until you give
it one.

`--init-chart` is the one explicit exception: on a graph with **no** chart it
authors a starter chart from the Mercury accounts and the accounts
`mapping.toml` names, and on later runs adds accounts that are missing. It
never removes an account, and it is never implied by a normal run. Prefer a
platform template when one fits your industry.

## Quickstart

```bash
# 1. Create your repo from this one (Use this template), then:
just venv                 # or: uv sync
cp .env.example .env      # fill in the four values below
uv run python -m integration.main --dry-run   # pulls Mercury, writes data/preview/events.json, sends nothing
uv run python -m integration.main             # registers the source, creates counterparties, emits
```

`.env`:

| Variable | Value |
|---|---|
| `ROBOSYSTEMS_API_KEY` | an API key for the graph (`X-API-Key`) |
| `ROBOSYSTEMS_GRAPH_ID` | the RoboLedger graph (`kg…`) |
| `MERCURY_API_KEY` | a **Read Only** Mercury API token (Settings → API tokens). Read-only tokens need no IP allowlist |
| `MERCURY_SINCE` | earliest transaction date, `YYYY-MM-DD` |

`ROBOSYSTEMS_API_URL` defaults to the managed cloud; point it at
`http://localhost:8000` for a local stack. `INTEGRATION_SOURCE_NAME` defaults
to `mercury`.

Flags: `--dry-run` (nothing sent), `--limit N` (emit the first N events, a
smoke test), `--from-snapshot data/raw/<ts>/mercury.json` (re-transform a
saved pull without calling Mercury), `--init-chart` (author the starter chart
on a graph that has none).

## Deploying

The included workflow (`.github/workflows/run.yml`) runs daily on GitHub
Actions with zero infrastructure. Set `secrets.ROBOSYSTEMS_API_KEY`,
`secrets.MERCURY_API_KEY`, `vars.ROBOSYSTEMS_GRAPH_ID` and
`vars.MERCURY_SINCE` in the repo settings and it is deployed. A daily run is
also what keeps the token alive: **Mercury deletes API tokens unused for 45
days.** GitHub pauses cron in public repos after 60 days of inactivity, so
keep the repo private or commit occasionally.

For larger runners or an ECS scheduled task see the template's deploy
ladder — the contract is env vars in, API calls out.

## What the platform does and does not know

- The connection shows on your graph's connections page as an external
  source named `mercury`. **The platform cannot trigger this sync** — you own
  the schedule — so the close's stale-sync check does not cover it, and there
  is no "reconnect" prompt when the token dies. Watch the Actions run.
- If your graph also syncs QuickBooks, Mercury events will sit beside the
  QuickBooks-mirrored ledger as unposted captures. That is the "bank as the
  independent check" shape; a matcher is not part of this integration. Do
  not classify and post them natively on a graph QuickBooks still owns.

## Layout

```
mapping.toml              # Tier-0 hints: Mercury signals → account names (yours to edit)
src/integration/
  config.py               # env-driven settings (.env supported)
  mercury.py              # Mercury API client (read-only token)
  collect.py              # pull + snapshot to data/raw/
  transform.py            # transactions → events; resolves accounts against your chart
  setup.py                # register source, read the chart, create agents (idempotent)
  emit/events.py          # create-event-block, per-event tolerant
  main.py                 # setup → collect → transform → emit
tests/                    # transform + client unit tests (no network)
```

Day-to-day: `just test`, `just test-all` (tests + format + lint + typecheck),
`just lint`, `just format`, `just typecheck`.

## SDK

Depends on [`robosystems-client`](https://pypi.org/project/robosystems-client/)
`>=1,<2`. The operations this integration uses — `create-event-block`,
`create-agent`, the connections API, GraphQL reads, and (`--init-chart` only)
`create-taxonomy-block` / `update-taxonomy-block` — are part of the SDK's
stable surface.

## License

MIT.
