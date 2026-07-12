"""Interactive Brokers Flex Query provider (custom, personal-use addition).

Interactive Brokers (https://www.interactivebrokers.com) exposes a read-only
reporting API called the "Flex Web Service". You define a Flex Query template
in Client Portal (Settings -> Reporting -> Flex Queries), enable the Flex Web
Service under Settings -> Reporting -> Flex Web Service to get an access
Token, and note the numeric Query ID of your template.

The Flex Web Service is a two-step polling protocol, not a live API:

  1. GET .../SendRequest?t={token}&q={queryId}&v=3
     -> returns a <ReferenceCode> identifying a freshly-generated report
        instance (the report itself is *not* in this response).
  2. GET .../GetStatement?t={token}&q={referenceCode}&v=3
     -> returns the actual report as XML once IB has finished generating it.
        Immediately after step 1 this can still return a "not ready" error
        (codes 1003/1004/1009); we poll with backoff.

This is intentionally NOT the live TWS/IB Gateway trading API — Flex data is
end-of-day / delayed (IB documents this), which is a fine trade-off for a
personal net-worth dashboard that doesn't need intraday prices for IBKR
specifically (Securo's ticker-based market pricing can layer live-ish prices
on top of the positions this provider reports).

Setup (one-time, in IBKR Client Portal):
  1. Settings -> Reporting -> Flex Queries -> create an "Activity Flex Query"
     including at least: Open Positions, Cash Transactions, Cash Report.
  2. Settings -> Reporting -> Flex Web Service -> generate a Token.
  3. Note the numeric Query ID of the template from step 1.

Connecting in Securo: this provider uses the generic paste-a-token flow
(same contract as SimpleFIN's ``handle_oauth_callback``). Since we need two
values (Token + Query ID) and the API only carries one opaque ``code``
string, we accept them as ``token:queryId`` (colon-separated). The frontend's
TokenConnectDialog (see token-connect-dialog.tsx) surfaces provider-specific
copy for this format via the ``accounts.tokenConnect.ibkr_flex.*`` i18n keys.
It can also be connected directly via the API, e.g.:

    curl -X POST https://finance.yourdomain/api/connections/oauth/callback \\
      -H "Content-Type: application/json" \\
      -H "Cookie: <your session cookie>" \\
      -d '{"provider": "ibkr_flex", "code": "<TOKEN>:<QUERY_ID>"}'

(Get the session cookie from your browser's dev tools while logged into
Securo.)
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from xml.etree import ElementTree as ET

import httpx

from app.agents.services.crypto import decrypt, encrypt
from app.providers.base import (
    AccountData,
    BankProvider,
    ConnectionData,
    HoldingData,
    ProviderUserActionRequired,
    SessionExpiredError,
    TransactionData,
)

logger = logging.getLogger(__name__)

SEND_REQUEST_URL = (
    "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
)
GET_STATEMENT_URL = (
    "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
)
FLEX_VERSION = "3"
HTTP_TIMEOUT = 30.0

# "Not ready yet, try again shortly" codes per IB's Flex Web Service docs.
# Anything else (bad token, bad query id, etc.) is a hard failure.
_RETRYABLE_CODES = {"1003", "1004", "1005", "1006", "1007", "1008", "1009"}
_MAX_POLL_ATTEMPTS = 8
_POLL_INTERVAL_SECONDS = 5

# How long we cache a fully-parsed statement on this provider instance.
# The sync layer creates one provider instance per sync_connection() call and
# calls get_accounts / get_holdings / get_transactions on it in sequence —
# caching avoids paying for three separate full Flex report generations
# (each involving multi-second polling) for what is functionally one read.
_CACHE_TTL_SECONDS = 120


class IbkrFlexAuthError(ProviderUserActionRequired):
    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            code="credentials_invalid",
            help_url=(
                "https://www.ibkrguides.com/clientportal/performanceandstatements/flex3.htm"
            ),
        )


def _parse_credentials(code: str) -> tuple[str, str]:
    """Split the pasted 'token:queryId' string into (token, query_id)."""
    cleaned = code.strip()
    if ":" not in cleaned:
        raise ValueError(
            "Expected credentials in 'TOKEN:QUERY_ID' format "
            "(your Flex Web Service token, a colon, then your numeric Query ID)"
        )
    token, _, query_id = cleaned.partition(":")
    token = token.strip()
    query_id = query_id.strip()
    if not token or not query_id.isdigit():
        raise ValueError(
            "Invalid IBKR Flex credentials: token must be non-empty and "
            "Query ID must be numeric"
        )
    return token, query_id


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _flex_date(value: Any) -> Optional[date]:
    """Parse IB Flex date formats: 'YYYYMMDD' or 'YYYYMMDD;HHMMSS'."""
    if not value:
        return None
    raw = str(value).split(";")[0].strip()
    if not re.fullmatch(r"\d{8}", raw):
        return None
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return None


class IbkrFlexProvider(BankProvider):
    """Interactive Brokers Flex Query connector (read-only, polling-based)."""

    def __init__(self) -> None:
        # keyed by (token, query_id) -> (fetched_at_monotonic, ET.Element root)
        self._cache: dict[tuple[str, str], tuple[float, ET.Element]] = {}

    @property
    def name(self) -> str:
        return "ibkr_flex"

    @property
    def flow_type(self) -> str:
        return "token"

    # ----- credentials handling ---------------------------------------------

    @staticmethod
    def _unpack(credentials: dict) -> tuple[str, str]:
        token_enc = (credentials or {}).get("token_enc")
        token = decrypt(token_enc) if token_enc else (credentials or {}).get("token")
        query_id = (credentials or {}).get("query_id")
        if not token or not query_id:
            raise SessionExpiredError("IBKR Flex token/query id missing")
        return str(token), str(query_id)

    # ----- Flex Web Service polling -----------------------------------------

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            headers={
                "User-Agent": "Securo/0.1 (+https://usesecuro.com)",
                "Accept": "application/xml",
            },
        )

    async def _send_request(self, token: str, query_id: str) -> str:
        async with await self._client() as client:
            resp = await client.get(
                SEND_REQUEST_URL,
                params={"t": token, "q": query_id, "v": FLEX_VERSION},
            )
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        status = (root.findtext("Status") or "").strip()
        if status != "Success":
            code = (root.findtext("ErrorCode") or "").strip()
            msg = (root.findtext("ErrorMessage") or "Unknown error").strip()
            if code in ("1001", "1002", "1020"):
                # Invalid token / invalid query id / query not found.
                raise IbkrFlexAuthError(f"IBKR Flex request rejected: {msg}")
            raise RuntimeError(f"IBKR Flex SendRequest failed ({code}): {msg}")
        ref_code = (root.findtext("ReferenceCode") or "").strip()
        if not ref_code:
            raise RuntimeError("IBKR Flex SendRequest returned no ReferenceCode")
        return ref_code

    async def _get_statement(self, token: str, query_id: str, ref_code: str) -> ET.Element:
        async with await self._client() as client:
            for attempt in range(_MAX_POLL_ATTEMPTS):
                resp = await client.get(
                    GET_STATEMENT_URL,
                    params={"t": token, "q": ref_code, "v": FLEX_VERSION},
                )
                resp.raise_for_status()
                root = ET.fromstring(resp.text)
                # A statement response wrapper means "not ready" or "error";
                # the real report's root tag is <FlexQueryResponse>.
                if root.tag == "FlexStatementResponse":
                    code = (root.findtext("ErrorCode") or "").strip()
                    msg = (root.findtext("ErrorMessage") or "").strip()
                    if code in _RETRYABLE_CODES and attempt < _MAX_POLL_ATTEMPTS - 1:
                        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                        continue
                    if code in ("1001", "1002", "1020"):
                        raise IbkrFlexAuthError(f"IBKR Flex rejected credentials: {msg}")
                    raise RuntimeError(f"IBKR Flex GetStatement failed ({code}): {msg}")
                return root
        raise RuntimeError(
            "IBKR Flex report did not become ready in time — try again in a minute"
        )

    async def _fetch_statement(self, credentials: dict) -> ET.Element:
        token, query_id = self._unpack(credentials)
        cache_key = (token, query_id)
        cached = self._cache.get(cache_key)
        if cached is not None:
            fetched_at, root = cached
            if (asyncio.get_event_loop().time() - fetched_at) < _CACHE_TTL_SECONDS:
                return root
        ref_code = await self._send_request(token, query_id)
        # IB explicitly recommends waiting before the first poll; reports
        # rarely finish instantly.
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        root = await self._get_statement(token, query_id, ref_code)
        self._cache[cache_key] = (asyncio.get_event_loop().time(), root)
        return root

    @staticmethod
    def _statement(root: ET.Element) -> ET.Element:
        stmt = root.find(".//FlexStatement")
        if stmt is None:
            raise RuntimeError("IBKR Flex report had no FlexStatement section")
        return stmt

    # ----- connection flow ---------------------------------------------------

    def get_oauth_url(self, *args, **kwargs):  # type: ignore[override]
        raise NotImplementedError(
            "IBKR Flex uses a paste-token:queryId flow, not an OAuth redirect"
        )

    async def handle_oauth_callback(self, code: str) -> ConnectionData:
        """Validate a pasted 'token:queryId' pair and build the connection.

        Mirrors SimpleFIN's reuse of the OAuth-callback contract for a
        non-OAuth, paste-credentials flow: given an opaque ``code`` string,
        produce a ``ConnectionData``.
        """
        token, query_id = _parse_credentials(code)
        root = await self._fetch_statement({"token": token, "query_id": query_id})
        stmt = self._statement(root)
        account_id = stmt.get("accountId") or f"ibkr-{query_id}"
        base_currency = stmt.get("currency") or "USD"

        credentials: dict[str, Any] = {
            "token_enc": encrypt(token) or token,
            "query_id": query_id,
        }

        accounts = self._parse_cash_account(stmt, base_currency)
        return ConnectionData(
            external_id=str(account_id),
            institution_name="Interactive Brokers",
            credentials=credentials,
            accounts=accounts,
            logo_url=None,
        )

    async def refresh_credentials(self, credentials: dict) -> dict:
        # The Flex token is a static long-lived credential (IB recommends
        # rotating it yourself periodically; there's no refresh-token dance).
        self._unpack(credentials)  # raises SessionExpiredError if missing
        return credentials

    # ----- reads --------------------------------------------------------------

    @staticmethod
    def _parse_cash_account(stmt: ET.Element, base_currency: str) -> list[AccountData]:
        """Build a single cash 'account' from the CashReport's base-currency
        summary row (CurrencyPrimary == 'BASE_SUMMARY'), if present.

        IBKR's real value lives in the positions (see get_holdings) — this
        account row exists mainly so Securo has somewhere to hang a currency
        + a running cash balance, same role a checking account plays for
        Pluggy/SimpleFIN connections that also carry investment holdings.
        """
        account_id = stmt.get("accountId") or "ibkr"
        balance = Decimal("0")
        for cash_row in stmt.iter("CashReportCurrency"):
            if cash_row.get("currency") == "BASE_SUMMARY":
                balance = _decimal(cash_row.get("endingCash")) or Decimal("0")
                break
        return [
            AccountData(
                external_id=f"{account_id}-cash",
                name="Interactive Brokers",
                type="checking",
                balance=balance,
                currency=base_currency,
            )
        ]

    async def get_accounts(self, credentials: dict) -> list[AccountData]:
        root = await self._fetch_statement(credentials)
        stmt = self._statement(root)
        base_currency = stmt.get("currency") or "USD"
        return self._parse_cash_account(stmt, base_currency)

    async def get_transactions(
        self,
        credentials: dict,
        account_external_id: str,
        since: Optional[date] = None,
        payee_source: str = "auto",
    ) -> list[TransactionData]:
        """Cash movements only (dividends, interest, fees, deposits/withdrawals).

        Trades (buys/sells) are not surfaced here — they're reflected as
        position changes via get_holdings, consistent with how Securo treats
        other investment-only connections (XP/Clear via Pluggy). If you want
        trade-level transaction history too, extend this to also parse the
        <Trades> section.
        """
        root = await self._fetch_statement(credentials)
        stmt = self._statement(root)
        transactions: list[TransactionData] = []
        for row in stmt.iter("CashTransaction"):
            txn_id = row.get("transactionID") or row.get("actionID")
            if not txn_id:
                continue
            amount = _decimal(row.get("amount"))
            if amount is None:
                continue
            txn_date = _flex_date(row.get("dateTime")) or _flex_date(row.get("reportDate"))
            if not txn_date:
                continue
            if since and txn_date < since:
                continue
            description = (
                row.get("description")
                or row.get("type")
                or "Interactive Brokers transaction"
            ).strip()[:500]
            transactions.append(
                TransactionData(
                    external_id=str(txn_id),
                    description=description,
                    amount=amount.copy_abs(),
                    date=txn_date,
                    type="credit" if amount >= 0 else "debit",
                    currency=row.get("currency"),
                    status="posted",
                    payee=row.get("type"),
                    raw_data=dict(row.attrib),
                )
            )
        return transactions

    async def get_holdings(self, credentials: dict) -> list[HoldingData]:
        root = await self._fetch_statement(credentials)
        stmt = self._statement(root)
        holdings: list[HoldingData] = []
        for row in stmt.iter("OpenPosition"):
            symbol = row.get("symbol")
            conid = row.get("conid")
            external_id = conid or symbol
            if not external_id:
                continue
            quantity = _decimal(row.get("position"))
            mark_price = _decimal(row.get("markPrice"))
            position_value = _decimal(row.get("positionValue"))
            if position_value is None and quantity is not None and mark_price is not None:
                position_value = quantity * mark_price
            if position_value is None:
                continue
            holdings.append(
                HoldingData(
                    external_id=str(external_id),
                    name=row.get("description") or symbol or str(external_id),
                    currency=row.get("currency") or "USD",
                    current_value=position_value,
                    quantity=quantity,
                    unit_price=mark_price,
                    purchase_price=_decimal(row.get("costBasisPrice")),
                    purchase_date=_flex_date(row.get("openDateTime")),
                    isin=row.get("isin"),
                    metadata={
                        "symbol": symbol,
                        "asset_category": row.get("assetCategory"),
                        "side": row.get("side"),
                        "cost_basis_money": row.get("costBasisMoney"),
                    },
                )
            )
        return holdings
