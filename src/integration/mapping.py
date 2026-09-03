"""The Tier-0 classification map (``mapping.toml``) as typed data."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path("mapping.toml")


@dataclass(frozen=True)
class AccountDef:
  qname: str
  name: str
  trait: str
  balance_type: str


@dataclass(frozen=True)
class Mapping:
  accounts: dict[str, AccountDef]
  custom_categories: dict[str, str]
  mercury_categories: dict[str, str]
  gl_codes: dict[str, str] = field(default_factory=dict)

  def gl_account(self, gl_code_name: str) -> AccountDef:
    """The account a Mercury GL code maps to — listed, or auto-derived."""
    qname = self.gl_codes.get(gl_code_name)
    if qname and qname in self.accounts:
      return self.accounts[qname]
    trait, balance = infer_trait(gl_code_name)
    return AccountDef(
      qname=f"mercury:gl_{slug(gl_code_name)}",
      name=gl_code_name,
      trait=trait,
      balance_type=balance,
    )


def load_mapping(path: Path = DEFAULT_PATH) -> Mapping:
  data = tomllib.loads(path.read_text())
  accounts = {
    qname: AccountDef(
      qname=qname,
      name=spec["name"],
      trait=spec["trait"],
      balance_type=spec.get("balance", "debit"),
    )
    for qname, spec in data.get("accounts", {}).items()
  }
  for section in ("custom_categories", "mercury_categories", "gl_codes"):
    for key, target in data.get(section, {}).items():
      if target and target not in accounts:
        raise ValueError(
          f"mapping.toml [{section}] {key!r} -> unknown account {target!r}"
        )
  return Mapping(
    accounts=accounts,
    custom_categories=dict(data.get("custom_categories", {})),
    mercury_categories=dict(data.get("mercury_categories", {})),
    gl_codes=dict(data.get("gl_codes", {})),
  )


_REVENUE = re.compile(r"revenue|income|sales|rewards|cashback|interest earned", re.I)
_LIABILITY = re.compile(r"payable|liabilit|loan|note|credit card|deferred", re.I)
_ASSET = re.compile(r"prepaid|receivable|asset|equipment|inventory|deposit", re.I)
_EQUITY = re.compile(r"equity|capital|contribution|distribution|draw", re.I)


def infer_trait(name: str) -> tuple[str, str]:
  """Best-effort (trait, balance_type) for an account we only know by name."""
  if _REVENUE.search(name):
    return "revenue", "credit"
  if _LIABILITY.search(name):
    return "liability", "credit"
  if _EQUITY.search(name):
    return "equity", "credit"
  if _ASSET.search(name):
    return "asset", "debit"
  return "expense", "debit"


def slug(value: str) -> str:
  """``General & Administrative:Technology`` -> ``GeneralAdministrativeTechnology``."""
  parts = re.split(r"[^A-Za-z0-9]+", value)
  return "".join(part[:1].upper() + part[1:] for part in parts if part)
