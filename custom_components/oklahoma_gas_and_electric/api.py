"""API client for Oklahoma Gas & Electric."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
import logging
import re
from typing import Any

from aiohttp import ClientError, ClientResponseError, ClientSession, ContentTypeError
from yarl import URL

from .const import (
    BASE_URL,
    GET_CHART_DATA_PATH,
    GET_ENERGY_ACCOUNTS_PATH,
    GET_ENERGY_ACCOUNT_DETAILS_PATH,
    GET_ESTIMATED_BILL_PATH,
    LOGIN_ACTION,
    LOGIN_PATH,
    LOGIN_PORTLET_ID,
    MAX_CHART_REQUEST_DAYS,
    PORTAL_PATH,
)

_LOGGER = logging.getLogger(__name__)
_SESSION_COOKIE_NAME = "JSESSIONID"
_P_AUTH_PATTERN = re.compile(r"p_auth=([A-Za-z0-9]+)")
_ESTIMATED_BILL_KEYS = {
    "estimatedbillamount",
    "estimatedbill",
    "billestimate",
    "estimatedamount",
    "amountdue",
}


@dataclass(slots=True, frozen=True)
class OgeAccount:
    """A single OGE account."""

    account_number: str
    contract_number: str | None
    service_address: str


@dataclass(slots=True, frozen=True)
class OgeAccountSnapshot:
    """Fetched data for a selected account."""

    account: OgeAccount
    estimated_bill: Decimal | None
    usage_days: tuple[OgeUsageDay, ...]
    details: dict[str, Any]


@dataclass(slots=True, frozen=True)
class OgeUsageHour:
    """Hourly usage data from OGE."""

    hour: str
    cost: Decimal
    kwh: Decimal
    kw_readings: tuple[Decimal, ...]

    @property
    def peak_kw(self) -> Decimal:
        """Return the highest quarter-hour demand for this hour."""
        if not self.kw_readings:
            return Decimal("0")
        return max(self.kw_readings)

    @property
    def has_usage(self) -> bool:
        """Return whether the hour contains non-zero usage or demand."""
        return (
            self.cost > 0
            or self.kwh > 0
            or any(reading > 0 for reading in self.kw_readings)
        )


@dataclass(slots=True, frozen=True)
class OgeUsageDay:
    """Daily usage data from OGE."""

    usage_date: date
    cost: Decimal
    kwh: Decimal
    hours: tuple[OgeUsageHour, ...]

    @property
    def peak_kw(self) -> Decimal:
        """Return the highest quarter-hour demand for the day."""
        if not self.hours:
            return Decimal("0")
        return max(hour.peak_kw for hour in self.hours)

    @property
    def latest_populated_hour(self) -> OgeUsageHour | None:
        """Return the latest hour with actual usage data."""
        for hour in reversed(self.hours):
            if hour.has_usage:
                return hour
        return None


class OgeClientError(Exception):
    """Base exception for OGE client failures."""


class OgeConnectionError(OgeClientError):
    """Raised when the OGE API cannot be reached."""


class OgeAuthenticationError(OgeClientError):
    """Raised when OGE credentials are invalid or expired."""


class OgeClient:
    """A client for the unofficial Oklahoma Gas & Electric API."""

    def __init__(
        self,
        session: ClientSession,
        username: str,
        password: str,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._username = username
        self._password = password

    async def async_get_accounts(self) -> list[OgeAccount]:
        """Fetch the accounts available to the authenticated user."""
        response = await self._async_request_json("GET", GET_ENERGY_ACCOUNTS_PATH)
        service_response = response.get("ServiceResponse")
        if not isinstance(service_response, dict):
            raise OgeClientError("Missing service response in GetEnergyAccounts payload")
        _raise_if_service_error(service_response)
        accounts = service_response.get("energyAccounts")
        if not isinstance(accounts, list):
            raise OgeClientError("Missing energy accounts in GetEnergyAccounts payload")

        parsed_accounts: list[OgeAccount] = []
        for account_data in accounts:
            if not isinstance(account_data, dict):
                continue
            account_number = _coerce_str(
                _find_value(
                    account_data,
                    "accountId",
                    "accountNumber",
                    "account_number",
                    "accountNo",
                    "account",
                )
            )
            if account_number is None:
                continue

            parsed_accounts.append(
                OgeAccount(
                    account_number=account_number,
                    contract_number=_coerce_str(
                        _find_value(
                            account_data,
                            "contractNumber",
                            "contract_number",
                            "contractNo",
                        )
                    ),
                    service_address=_extract_service_address(account_data)
                    or account_number,
                )
            )

        if not parsed_accounts:
            raise OgeClientError("No usable accounts returned by OGE")
        return parsed_accounts

    async def async_prepare_authenticated_session(self) -> None:
        """Start a fresh authenticated portal session."""
        self._clear_session_cookie()
        await self.async_login()

    async def async_get_account_snapshot(
        self,
        account: OgeAccount,
        from_date: date,
        to_date: date,
    ) -> OgeAccountSnapshot:
        """Fetch the latest data for a configured account."""
        accounts = await self.async_get_accounts()
        selected_account = next(
            (
                candidate
                for candidate in accounts
                if candidate.account_number == account.account_number
            ),
            account,
        )

        details = await self._async_try_request_json(
            "GET", GET_ENERGY_ACCOUNT_DETAILS_PATH
        )
        estimated_bill_payload = await self._async_try_request_json(
            "GET", GET_ESTIMATED_BILL_PATH
        )

        return OgeAccountSnapshot(
            account=selected_account,
            estimated_bill=_extract_estimated_bill(estimated_bill_payload),
            usage_days=await self.async_get_usage_days(
                selected_account, from_date, to_date
            ),
            details=details,
        )

    async def async_get_usage_days(
        self,
        account: OgeAccount,
        from_date: date,
        to_date: date,
    ) -> tuple[OgeUsageDay, ...]:
        """Fetch usage days for the requested range."""
        usage_days_by_date: dict[date, OgeUsageDay] = {}
        request_start = from_date
        while request_start <= to_date:
            request_end = min(
                request_start + timedelta(days=MAX_CHART_REQUEST_DAYS - 1), to_date
            )
            chart_payload = await self._async_request_json(
                "POST",
                GET_CHART_DATA_PATH,
                json_data={
                    "accountNumber": account.account_number,
                    "fromDate": request_start.isoformat(),
                    "toDate": request_end.isoformat(),
                },
            )
            for usage_day in _extract_usage_days(chart_payload):
                usage_days_by_date[usage_day.usage_date] = usage_day
            request_start = request_end + timedelta(days=1)

        return tuple(usage_days_by_date[day] for day in sorted(usage_days_by_date))

    async def _async_try_request_json(
        self, method: str, path: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Attempt an API request and return an empty payload on non-auth failures."""
        try:
            return await self._async_request_json(method, path, data)
        except OgeAuthenticationError:
            raise
        except OgeClientError as err:
            _LOGGER.debug("Optional OGE endpoint %s failed: %s", path, err)
            return {}

    async def _async_request_json(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Request and decode JSON from the OGE API."""
        for attempt in range(2):
            if not self._has_session_cookie():
                await self.async_login()

            try:
                async with self._session.request(
                    method,
                    f"{BASE_URL}{path}",
                    data=data,
                    json=json_data,
                    allow_redirects=True,
                ) as response:
                    if response.status in (401, 403):
                        self._clear_session_cookie()
                        if attempt == 0:
                            continue
                        raise OgeAuthenticationError("OGE rejected the session")

                    response.raise_for_status()

                    try:
                        payload: Any = await response.json(content_type=None)
                    except ContentTypeError:
                        payload = json.loads(await response.text())
            except ClientResponseError as err:
                if err.status in (401, 403):
                    self._clear_session_cookie()
                    if attempt == 0:
                        continue
                    raise OgeAuthenticationError("OGE rejected the session") from err
                raise OgeConnectionError(f"OGE request failed with status {err.status}") from err
            except ClientError as err:
                raise OgeConnectionError("Failed to connect to OGE") from err
            except json.JSONDecodeError as err:
                raise OgeClientError("OGE returned invalid JSON") from err

            decoded_payload = _decode_json_like(payload)
            if not isinstance(decoded_payload, dict):
                raise OgeClientError("OGE returned an unexpected payload shape")
            return decoded_payload

        raise OgeAuthenticationError("OGE authentication failed")

    async def async_login(self) -> None:
        """Authenticate against the OGE portal and store the session cookie."""
        try:
            async with self._session.get(
                f"{BASE_URL}{PORTAL_PATH}",
                allow_redirects=True,
            ) as portal_response:
                portal_response.raise_for_status()
                portal_html = await portal_response.text()

            p_auth_match = _P_AUTH_PATTERN.search(portal_html)
            if p_auth_match is None:
                raise OgeAuthenticationError("OGE portal auth token not found")

            async with self._session.post(
                f"{BASE_URL}{LOGIN_PATH}",
                params={
                    "p_p_id": LOGIN_PORTLET_ID,
                    "p_p_lifecycle": "1",
                    f"_{LOGIN_PORTLET_ID}_javax.portlet.action": LOGIN_ACTION,
                    "p_auth": p_auth_match.group(1),
                },
                data={
                    "userName": self._username,
                    "password": self._password,
                },
                headers={
                    "Origin": BASE_URL,
                    "Referer": f"{BASE_URL}{PORTAL_PATH}",
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:151.0) "
                        "Gecko/20100101 Firefox/151.0"
                    ),
                },
                allow_redirects=True,
            ) as response:
                response.raise_for_status()
                await response.read()
        except ClientResponseError as err:
            raise OgeConnectionError(f"OGE login failed with status {err.status}") from err
        except ClientError as err:
            raise OgeConnectionError("Failed to connect to OGE login") from err

        if not self._has_session_cookie():
            raise OgeAuthenticationError("OGE login did not create a session")

    def _has_session_cookie(self) -> bool:
        """Return whether the OGE session cookie is available."""
        cookies = self._session.cookie_jar.filter_cookies(URL(BASE_URL))
        return _SESSION_COOKIE_NAME in cookies

    def _clear_session_cookie(self) -> None:
        """Clear the OGE session cookie jar."""
        self._session.cookie_jar.clear()


def _decode_json_like(value: Any) -> Any:
    """Decode nested JSON strings found in OGE responses."""
    if isinstance(value, dict):
        return {key: _decode_json_like(subvalue) for key, subvalue in value.items()}
    if isinstance(value, list):
        return [_decode_json_like(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in {"{", "["}:
            try:
                return _decode_json_like(json.loads(stripped))
            except json.JSONDecodeError:
                return value
    return value


def _find_value(data: dict[str, Any], *keys: str) -> Any:
    """Find the first value matching one of the provided keys."""
    normalized_keys = {_normalize_key(key) for key in keys}
    for key, value in data.items():
        if _normalize_key(key) in normalized_keys:
            return value
    return None


def _normalize_key(value: str) -> str:
    """Normalize keys for case-insensitive comparisons."""
    return "".join(char for char in value.lower() if char.isalnum())


def _coerce_str(value: Any) -> str | None:
    """Convert a value into a clean string."""
    if value is None:
        return None
    coerced = str(value).strip()
    return coerced or None


def _extract_service_address(account_data: dict[str, Any]) -> str | None:
    """Build a readable service address from account data."""
    direct_value = _find_value(
        account_data,
        "serviceAddress",
        "service_address",
        "serviceLocation",
    )
    if isinstance(direct_value, str):
        return direct_value.strip() or None
    if isinstance(direct_value, dict):
        return _join_address_parts(direct_value.values())

    address_parts = [
        _coerce_str(value)
        for key, value in account_data.items()
        if "address" in _normalize_key(key)
    ]
    return _join_address_parts(address_parts)


def _join_address_parts(parts: Any) -> str | None:
    """Join address parts while skipping blanks."""
    values = [part for part in (_coerce_str(part) for part in parts) if part]
    if not values:
        return None
    return ", ".join(values)


def _extract_estimated_bill(payload: dict[str, Any]) -> Decimal | None:
    """Extract an estimated bill amount from a response payload."""
    for key_path, value in _flatten(payload):
        if not key_path:
            continue
        normalized_key = _normalize_key(key_path[-1])
        if normalized_key not in _ESTIMATED_BILL_KEYS:
            continue
        decimal_value = _coerce_decimal(value)
        if decimal_value is not None:
            return decimal_value

    return None


def _extract_usage_days(payload: dict[str, Any]) -> tuple[OgeUsageDay, ...]:
    """Extract charted usage days from the OGE chart payload."""
    service_response = payload.get("ServiceResponse")
    if not isinstance(service_response, dict):
        raise OgeClientError("Missing service response in GetChartData payload")
    _raise_if_service_error(service_response)

    day_payloads = service_response.get("days")
    if not isinstance(day_payloads, list):
        raise OgeClientError("Missing days in GetChartData payload")

    usage_days: list[OgeUsageDay] = []
    for day_payload in day_payloads:
        if not isinstance(day_payload, dict):
            continue
        usage_date_raw = day_payload.get("date")
        if not isinstance(usage_date_raw, str):
            continue

        hours: list[OgeUsageHour] = []
        hour_payloads = day_payload.get("hours")
        if isinstance(hour_payloads, list):
            for hour_payload in hour_payloads:
                if not isinstance(hour_payload, dict):
                    continue
                kw_payload = hour_payload.get("kw")
                kw_readings: tuple[Decimal, ...] = ()
                if isinstance(kw_payload, list):
                    kw_readings = tuple(
                        _coerce_decimal(reading) or Decimal("0") for reading in kw_payload
                    )
                hours.append(
                    OgeUsageHour(
                        hour=_coerce_str(hour_payload.get("hour")) or "",
                        cost=_coerce_decimal(hour_payload.get("cost")) or Decimal("0"),
                        kwh=_coerce_decimal(hour_payload.get("kwh")) or Decimal("0"),
                        kw_readings=kw_readings,
                    )
                )

        usage_days.append(
            OgeUsageDay(
                usage_date=date.fromisoformat(usage_date_raw),
                cost=_coerce_decimal(day_payload.get("cost")) or Decimal("0"),
                kwh=_coerce_decimal(day_payload.get("kwh")) or Decimal("0"),
                hours=tuple(hours),
            )
        )

    if not usage_days:
        raise OgeClientError("No usage days returned by OGE")

    return tuple(sorted(usage_days, key=lambda usage_day: usage_day.usage_date))


def _flatten(value: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    """Flatten a nested JSON-compatible payload into key/value pairs."""
    flattened: list[tuple[tuple[str, ...], Any]] = []
    if isinstance(value, dict):
        for key, subvalue in value.items():
            flattened.extend(_flatten(subvalue, (*path, key)))
    elif isinstance(value, list):
        for item in value:
            flattened.extend(_flatten(item, path))
    else:
        flattened.append((path, value))
    return flattened


def _coerce_decimal(value: Any) -> Decimal | None:
    """Convert a payload value into a decimal."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        cleaned = value.replace("$", "").replace(",", "").strip()
        if not cleaned:
            return None
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


def _raise_if_service_error(service_response: dict[str, Any]) -> None:
    """Raise an auth error for error-shaped OGE service responses."""
    error_code = _coerce_str(service_response.get("errorCode"))
    error_message = _coerce_str(service_response.get("errorMessage"))
    if error_code == "401":
        raise OgeAuthenticationError(error_message or "Unauthorized")
