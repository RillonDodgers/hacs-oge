"""The Oklahoma Gas & Electric integration."""

import asyncio
from collections.abc import Iterable
from aiohttp import CookieJar
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers import config_validation as cv

from .api import OgeAccount, OgeAuthenticationError, OgeClient, OgeConnectionError
from .const import CONF_ACCOUNT_NUMBER, CONF_CONTRACT_NUMBER, DOMAIN
from .coordinator import OgeConfigEntry, OgeDataUpdateCoordinator

_PLATFORMS: list[Platform] = [Platform.SENSOR]
SERVICE_REFRESH = "refresh"
SERVICE_REFRESH_SCHEMA = vol.Schema({vol.Optional(CONF_ACCOUNT_NUMBER): cv.string})


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
        coordinators = list(
            _iter_coordinators(
                hass.config_entries.async_entries(DOMAIN),
                target_account=target_account,
            )
        )
        await asyncio.gather(
            *(coordinator.async_request_refresh() for coordinator in coordinators)
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
