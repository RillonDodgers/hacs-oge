"""The Oklahoma Gas & Electric integration."""

from aiohttp import CookieJar

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import OgeAccount, OgeAuthenticationError, OgeClient, OgeConnectionError
from .const import CONF_ACCOUNT_NUMBER, CONF_CONTRACT_NUMBER
from .coordinator import OgeConfigEntry, OgeDataUpdateCoordinator

_PLATFORMS: list[Platform] = [Platform.SENSOR]


def _build_fallback_account(entry: ConfigEntry) -> OgeAccount:
    """Build an account object from stored config entry data."""
    return OgeAccount(
        account_number=entry.data[CONF_ACCOUNT_NUMBER],
        contract_number=entry.data.get(CONF_CONTRACT_NUMBER),
        service_address=entry.title,
    )


async def async_setup_entry(hass: HomeAssistant, entry: OgeConfigEntry) -> bool:
    """Set up Oklahoma Gas & Electric from a config entry."""
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
