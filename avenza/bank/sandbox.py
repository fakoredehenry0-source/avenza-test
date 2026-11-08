from __future__ import annotations

import os
from datetime import datetime

import httpx

from ..config import Settings
from .adapter import AccountInfo, BankAdapter, BankNotFound, BankResult, BankTimeout, BankUnavailable


class SandboxBank(BankAdapter):
    """Organisers' sandbox adapter.

    CONFIRM: every endpoint path, auth scheme, payload field and status string
    below is a placeholder. The organisers' sandbox documentation was not
    available when this build started. Do not claim this adapter works until
    it has been exercised against the real sandbox. BANK_MODE=sandbox refuses
    to send anything unless SANDBOX_BASE_URL and SANDBOX_API_KEY are set.
    """

    name = "sandbox"
    simulated = False

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = os.getenv("SANDBOX_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("SANDBOX_API_KEY", "")
        # CONFIRM these paths with the organisers.
        self.name_path = os.getenv("SANDBOX_NAME_PATH", "/accounts/name-enquiry")
        self.transfer_path = os.getenv("SANDBOX_TRANSFER_PATH", "/transfers")
        self.status_path = os.getenv("SANDBOX_STATUS_PATH", "/transfers/{reference}")
        self.history_path = os.getenv("SANDBOX_HISTORY_PATH", "/transfers")
        self.receipt_path = os.getenv("SANDBOX_RECEIPT_PATH", "/transfers/{reference}/receipt")

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _require_ready(self) -> None:
        if not self.ready:
            raise BankUnavailable("sandbox is not configured (CONFIRM organisers' endpoints)")

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        self._require_ready()
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.request(method, url, headers=self._headers(), **kwargs)
        except httpx.TimeoutException as exc:
            raise BankTimeout("sandbox request timed out") from exc
        except httpx.HTTPError as exc:
            raise BankUnavailable(f"sandbox unavailable: {type(exc).__name__}") from exc
        if resp.status_code == 404:
            raise BankNotFound("sandbox account or transfer not found")
        if resp.status_code >= 500:
            raise BankUnavailable(f"sandbox error {resp.status_code}")
        if resp.status_code >= 400:
            raise BankUnavailable(f"sandbox rejected request {resp.status_code}: {resp.text[:200]}")
        return resp

    async def name_enquiry(self, bank_code: str, account_number: str) -> AccountInfo:
        resp = await self._request(
            "POST",
            f"{self.base_url}{self.name_path}",
            json={"bank_code": bank_code, "account_number": account_number},
        )
        data = resp.json()
        # CONFIRM response field names.
        return AccountInfo(
            bank_code=bank_code,
            bank_name=data.get("bank_name", bank_code),
            account_number=data.get("account_number", account_number),
            account_name=data.get("account_name") or data.get("name", ""),
        )

    async def create_transfer(
        self,
        *,
        idempotency_key: str,
        customer_ref: str,
        amount_kobo: int,
        bank_code: str,
        account_number: str,
        recipient_name: str,
        nonce: str,
    ) -> BankResult:
        resp = await self._request(
            "POST",
            f"{self.base_url}{self.transfer_path}",
            headers={**self._headers(), "Idempotency-Key": idempotency_key},
            json={
                "idempotency_key": idempotency_key,
                "customer_ref": customer_ref,
                "amount_kobo": amount_kobo,
                "currency": "NGN",
                "bank_code": bank_code,
                "account_number": account_number,
                "recipient_name": recipient_name,
                "nonce": nonce,
            },
        )
        data = resp.json()
        # CONFIRM status mapping.
        raw_status = str(data.get("status", "")).upper()
        status = {
            "SUCCESS": "SETTLED",
            "SETTLED": "SETTLED",
            "COMPLETED": "SETTLED",
            "FAILED": "FAILED",
            "PENDING": "PENDING",
            "PROCESSING": "PENDING",
        }.get(raw_status, "PENDING")
        return BankResult(
            status=status,
            reference=data.get("reference") or data.get("transaction_reference"),
            amount_kobo=int(data.get("amount_kobo", amount_kobo)),
            account_number=account_number,
            recipient_name=recipient_name,
            failure_reason=data.get("failure_reason"),
            raw=data,
        )

    async def get_transfer_status(
        self, reference: str | None = None, idempotency_key: str | None = None
    ) -> BankResult:
        if reference:
            url = f"{self.base_url}{self.status_path.format(reference=reference)}"
            resp = await self._request("GET", url)
        elif idempotency_key:
            url = f"{self.base_url}{self.transfer_path}"
            resp = await self._request("GET", url, params={"idempotency_key": idempotency_key})
        else:
            return BankResult(status="NOT_FOUND", reference=None)
        data = resp.json()
        raw_status = str(data.get("status", "")).upper()
        status = {
            "SUCCESS": "SETTLED",
            "SETTLED": "SETTLED",
            "COMPLETED": "SETTLED",
            "FAILED": "FAILED",
            "PENDING": "PENDING",
            "PROCESSING": "PENDING",
        }.get(raw_status, "PENDING")
        return BankResult(
            status=status,
            reference=data.get("reference") or reference,
            amount_kobo=int(data.get("amount_kobo", 0)),
            account_number=data.get("account_number", ""),
            recipient_name=data.get("recipient_name", ""),
            failure_reason=data.get("failure_reason"),
            raw=data,
        )

    async def list_transactions(
        self, customer_ref: str, start: datetime, end: datetime, limit: int = 50
    ) -> list[BankResult]:
        resp = await self._request(
            "GET",
            f"{self.base_url}{self.history_path}",
            params={
                "customer_ref": customer_ref,
                "from": start.isoformat(),
                "to": end.isoformat(),
                "limit": limit,
            },
        )
        data = resp.json()
        items = data if isinstance(data, list) else data.get("items", data.get("transactions", []))
        results: list[BankResult] = []
        for item in items:
            results.append(
                BankResult(
                    status=str(item.get("status", "")).upper(),
                    reference=item.get("reference"),
                    amount_kobo=int(item.get("amount_kobo", 0)),
                    account_number=item.get("account_number", ""),
                    recipient_name=item.get("recipient_name", ""),
                    failure_reason=item.get("failure_reason"),
                    raw=item,
                )
            )
        return results

    async def get_receipt(self, reference: str) -> dict | None:
        resp = await self._request("GET", f"{self.base_url}{self.receipt_path.format(reference=reference)}")
        return resp.json()
