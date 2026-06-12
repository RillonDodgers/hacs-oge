"""Data coordinator for Oklahoma Gas & Electric."""

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    OgeAccount,
    OgeAccountSnapshot,
    OgeAuthenticationError,
    OgeClient,
    OgeClientError,
    OgeConnectionError,
    OgeUsageDay,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CORRECTION_DAYS,
    CONF_HISTORY_DAYS,
    CONF_POLL_INTERVAL_HOURS,
    DEFAULT_CORRECTION_DAYS,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_POLL_INTERVAL_HOURS,
    DOMAIN,
)
from .statistics import OgeStatisticsManager

type OgeConfigEntry = ConfigEntry[OgeDataUpdateCoordinator]

_LOGGER = logging.getLogger(__name__)


def _get_update_interval(config_entry: ConfigEntry) -> timedelta:
    """Return polling interval from config entry options."""
    hours = config_entry.options.get(
        CONF_POLL_INTERVAL_HOURS, DEFAULT_POLL_INTERVAL_HOURS
    )
    return timedelta(hours=hours)


@dataclass(slots=True, frozen=True)
class OgeData:
    """Coordinator data."""

    account: OgeAccount
    estimated_bill: Decimal | None
    usage_days: tuple[OgeUsageDay, ...]
    details: dict[str, Any]


class OgeDataUpdateCoordinator(DataUpdateCoordinator[OgeData]):
    """Coordinate OGE updates."""

    config_entry: OgeConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        api: OgeClient,
        config_entry: OgeConfigEntry,
        account: OgeAccount,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=_get_update_interval(config_entry),
            config_entry=config_entry,
        )
        self.api = api
        self.account = account
        self._statistics = OgeStatisticsManager(hass, account)
        self._manual_refresh_lock = asyncio.Lock()

    async def _async_update_data(self) -> OgeData:
        """Fetch data from OGE."""
        to_date = dt_util.now().date()
        history_days = self.config_entry.options.get(
            CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS
        )
        correction_days = self.config_entry.options.get(
            CONF_CORRECTION_DAYS, DEFAULT_CORRECTION_DAYS
        )
        has_imported_statistics = await self._statistics.async_has_statistics()
        lookback_days = history_days if not has_imported_statistics else correction_days
        from_date = to_date - timedelta(days=lookback_days - 1)
        _LOGGER.debug(
            "Scheduled OGE refresh for %s using %s window: %s..%s",
            self.account.account_number,
            "initial history" if not has_imported_statistics else "correction",
            from_date,
            to_date,
        )
        return await self._async_fetch_window(from_date, to_date)

    async def async_refresh_for_window(
        self,
        from_date: date,
        to_date: date,
    ) -> None:
        """Refresh coordinator using an explicit date window."""
        async with self._manual_refresh_lock:
            async with self._debounced_refresh.async_lock():
                _LOGGER.debug(
                    "Manual OGE refresh for %s using explicit window %s..%s",
                    self.account.account_number,
                    from_date,
                    to_date,
                )
                data = await self._async_fetch_window(from_date, to_date)
                self.async_set_updated_data(data)

    async def _async_fetch_window(
        self,
        from_date: date,
        to_date: date,
    ) -> OgeData:
        """Fetch and import data for a specific date window."""
        _LOGGER.debug(
            "Fetching OGE data for %s from %s to %s",
            self.account.account_number,
            from_date,
            to_date,
        )
        try:
            await self.api.async_prepare_authenticated_session()
            snapshot: OgeAccountSnapshot = await self.api.async_get_account_snapshot(
                self.account,
                from_date,
                to_date,
            )
        except OgeAuthenticationError as err:
            raise ConfigEntryAuthFailed from err
        except OgeConnectionError as err:
            raise UpdateFailed("Unable to connect to OGE") from err
        except OgeClientError as err:
            raise UpdateFailed(str(err)) from err

        self.account = snapshot.account
        self._statistics.account = snapshot.account
        await self._statistics.async_import_usage(snapshot.usage_days)
        _LOGGER.debug(
            "Fetched OGE data for %s: %s day%s imported",
            snapshot.account.account_number,
            len(snapshot.usage_days),
            "" if len(snapshot.usage_days) == 1 else "s",
        )
        return OgeData(
            account=snapshot.account,
            estimated_bill=snapshot.estimated_bill,
            usage_days=snapshot.usage_days,
            details=snapshot.details,
        )
