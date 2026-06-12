"""The Oklahoma Gas & Electric integration."""

import asyncio
from collections.abc import Iterable
from datetime import date, timedelta
import logging
from aiohttp import CookieJar
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import OgeAccount, OgeAuthenticationError, OgeClient, OgeConnectionError
from .const import CONF_ACCOUNT_NUMBER, CONF_CONTRACT_NUMBER, DOMAIN
from .coordinator import OgeConfigEntry, OgeDataUpdateCoordinator

_PLATFORMS: list[Platform] = [Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)
SERVICE_REFRESH = "refresh"
ATTR_FROM_DATE = "from_date"
ATTR_HISTORY_DAYS = "history_days"
ATTR_TO_DATE = "to_date"
SERVICE_REFRESH_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ACCOUNT_NUMBER): cv.string,
        vol.Optional(ATTR_HISTORY_DAYS): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional(ATTR_FROM_DATE): cv.date,
        vol.Optional(ATTR_TO_DATE): cv.date,
    }
)


def _build_fallback_account(entry: ConfigEntry) -> OgeAccount:
    """Build an account object from stored config entry data."""
    return OgeAccount(
        account_number=entry.data[CONF_ACCOUNT_NUMBER],
        contract_number=entry.data.get(CONF_CONTRACT_NUMBER),
        service_address=entry.title,
    )


async def async_setup_entry(hass: HomeAssistant, entry: OgeConfigEntry) -> bool:
    """Set up Oklahoma Gas & Electric from a config entry."""
    _async_register_services(hass)
    _LOGGER.debug("Setting up OGE entry for account %s", entry.data[CONF_ACCOUNT_NUMBER])
    client = OgeClient(
        session=async_create_clientsession(
            hass,
            cookie_jar=CookieJar(quote_cookie=False),
        ),
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )

    try:
        accounts = await client.async_get_accounts()
    except OgeAuthenticationError as err:
        raise ConfigEntryAuthFailed from err
    except OgeConnectionError as err:
        raise ConfigEntryNotReady from err

    account = next(
        (
            candidate
            for candidate in accounts
            if candidate.account_number == entry.data[CONF_ACCOUNT_NUMBER]
        ),
        _build_fallback_account(entry),
    )

    entry.runtime_data = coordinator = OgeDataUpdateCoordinator(
        hass=hass,
        api=client,
        config_entry=entry,
        account=account,
    )
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OgeConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration services."""
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH):
        return

    async def async_handle_refresh(call) -> None:
        """Refresh one or all configured OGE entries."""
        target_account = call.data.get(CONF_ACCOUNT_NUMBER)
        from_date, to_date = _get_refresh_window(call.data)
        coordinators = list(
            _iter_coordinators(
                hass.config_entries.async_entries(DOMAIN),
                target_account=target_account,
            )
        )
        _LOGGER.debug(
            "Manual OGE refresh requested for %s entry%s; target_account=%s; from_date=%s; to_date=%s",
            len(coordinators),
            "" if len(coordinators) == 1 else "ies",
            target_account,
            from_date,
            to_date,
        )
        if from_date is None or to_date is None:
            await asyncio.gather(
                *(coordinator.async_request_refresh() for coordinator in coordinators)
            )
            return

        await asyncio.gather(
            *(
                coordinator.async_refresh_for_window(from_date, to_date)
                for coordinator in coordinators
            )
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH,
        async_handle_refresh,
        schema=SERVICE_REFRESH_SCHEMA,
    )


def _iter_coordinators(
    entries: Iterable[OgeConfigEntry],
    target_account: str | None,
) -> Iterable[OgeDataUpdateCoordinator]:
    """Yield matching loaded coordinators."""
    for entry in entries:
        coordinator = entry.runtime_data
        if coordinator is None:
            continue
        if (
            target_account is not None
            and entry.data[CONF_ACCOUNT_NUMBER] != target_account
        ):
            continue
        yield coordinator


def _get_refresh_window(
    data: dict,
) -> tuple[date | None, date | None]:
    """Validate and derive manual refresh window overrides."""
    history_days = data.get(ATTR_HISTORY_DAYS)
    from_date = data.get(ATTR_FROM_DATE)
    to_date = data.get(ATTR_TO_DATE)

    if history_days is not None and (from_date is not None or to_date is not None):
        raise ServiceValidationError(
            "Use either history_days or from_date/to_date, not both"
        )

    if (from_date is None) ^ (to_date is None):
        raise ServiceValidationError(
            "from_date and to_date must be provided together"
        )

    if history_days is not None:
        to_date = dt_util.now().date()
        from_date = to_date - timedelta(days=history_days - 1)
        _LOGGER.debug(
            "Resolved manual OGE refresh window from history_days=%s to %s..%s",
            history_days,
            from_date,
            to_date,
        )
        return from_date, to_date

    if from_date is not None and to_date is not None:
        if from_date > to_date:
            raise ServiceValidationError("from_date must be on or before to_date")
        _LOGGER.debug(
            "Resolved manual OGE refresh explicit window %s..%s",
            from_date,
            to_date,
        )
        return from_date, to_date

    return None, None
