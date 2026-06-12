"""Config flow for the Oklahoma Gas & Electric integration."""

from collections.abc import Mapping
import logging
from typing import Any

from aiohttp import CookieJar
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow as ConfigFlowBase,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import (
    OgeAccount,
    OgeAuthenticationError,
    OgeClient,
    OgeClientError,
    OgeConnectionError,
)
from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_CONTRACT_NUMBER,
    CONF_CORRECTION_DAYS,
    CONF_HISTORY_DAYS,
    CONF_POLL_INTERVAL_HOURS,
    DEFAULT_CORRECTION_DAYS,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_POLL_INTERVAL_HOURS,
    DOMAIN,
    MAX_POLL_INTERVAL_HOURS,
    MIN_POLL_INTERVAL_HOURS,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): selector.TextSelector(),
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)


def _account_title(account: OgeAccount) -> str:
    """Return a friendly account title."""
    if account.service_address and account.service_address != account.account_number:
        return account.service_address
    return account.account_number


class ConfigFlow(ConfigFlowBase, domain=DOMAIN):
    """Handle a config flow for Oklahoma Gas & Electric."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._accounts: list[OgeAccount] = []
        self._user_input: dict[str, Any] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OgeOptionsFlowHandler:
        """Return the options flow handler."""
        return OgeOptionsFlowHandler()

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauthentication."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            client = OgeClient(
                session=async_create_clientsession(
                    self.hass,
                    cookie_jar=CookieJar(quote_cookie=False),
                ),
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
            )
            try:
                await client.async_get_accounts()
            except OgeAuthenticationError:
                errors["base"] = "invalid_auth"
            except OgeConnectionError:
                errors["base"] = "cannot_connect"
            except OgeClientError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception during OGE reauth flow")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates=user_input,
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA,
                {
                    CONF_USERNAME: reauth_entry.data[CONF_USERNAME],
                    CONF_PASSWORD: reauth_entry.data[CONF_PASSWORD],
                },
            ),
            errors=errors,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            client = OgeClient(
                session=async_create_clientsession(
                    self.hass,
                    cookie_jar=CookieJar(quote_cookie=False),
                ),
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
            )
            try:
                self._accounts = await client.async_get_accounts()
            except OgeAuthenticationError:
                errors["base"] = "invalid_auth"
            except OgeConnectionError:
                errors["base"] = "cannot_connect"
            except OgeClientError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception during OGE config flow")
                errors["base"] = "unknown"
            else:
                self._user_input = user_input
                if len(self._accounts) == 1:
                    return await self._async_create_account_entry(self._accounts[0])
                return await self.async_step_select_account()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_select_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle selecting an account."""
        if user_input is not None:
            selected_account = next(
                (
                    account
                    for account in self._accounts
                    if account.account_number == user_input[CONF_ACCOUNT_NUMBER]
                ),
                None,
            )
            if selected_account is not None:
                return await self._async_create_account_entry(selected_account)

        return self.async_show_form(
            step_id="select_account",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACCOUNT_NUMBER): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=account.account_number,
                                    label=(
                                        f"{account.account_number} - {account.service_address}"
                                        if account.service_address
                                        != account.account_number
                                        else account.account_number
                                    ),
                                )
                                for account in self._accounts
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def _async_create_account_entry(
        self, account: OgeAccount
    ) -> ConfigFlowResult:
        """Create the config entry for a selected account."""
        await self.async_set_unique_id(account.account_number)
        self._abort_if_unique_id_configured()
        assert self._user_input is not None

        return self.async_create_entry(
            title=_account_title(account),
            data={
                **self._user_input,
                CONF_ACCOUNT_NUMBER: account.account_number,
                CONF_CONTRACT_NUMBER: account.contract_number,
            },
        )


class OgeOptionsFlowHandler(OptionsFlowWithReload):
    """Handle OGE options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage OGE options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HISTORY_DAYS,
                        default=self.config_entry.options.get(
                            CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                    vol.Required(
                        CONF_CORRECTION_DAYS,
                        default=self.config_entry.options.get(
                            CONF_CORRECTION_DAYS, DEFAULT_CORRECTION_DAYS
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=90)),
                    vol.Required(
                        CONF_POLL_INTERVAL_HOURS,
                        default=self.config_entry.options.get(
                            CONF_POLL_INTERVAL_HOURS, DEFAULT_POLL_INTERVAL_HOURS
                        ),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=MIN_POLL_INTERVAL_HOURS,
                            max=MAX_POLL_INTERVAL_HOURS,
                        ),
                    ),
                }
            ),
        )
