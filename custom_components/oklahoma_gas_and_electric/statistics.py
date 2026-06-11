"""Recorder statistics helpers for Oklahoma Gas & Electric."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import CURRENCY_DOLLAR, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .api import OgeAccount, OgeUsageDay, OgeUsageHour
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class OgeImportedHour:
    """Normalized OGE hourly data."""

    start: datetime
    cost: Decimal
    kwh: Decimal
    peak_kw: Decimal


class OgeStatisticsManager:
    """Import OGE hourly usage into recorder statistics."""

    def __init__(self, hass: HomeAssistant, account: OgeAccount) -> None:
        """Initialize the statistics manager."""
        self.hass = hass
        self.account = account
        id_prefix = account.account_number.lower()
        self.energy_statistic_id = f"{DOMAIN}:{id_prefix}_energy_import"
        self.cost_statistic_id = f"{DOMAIN}:{id_prefix}_energy_cost"

    async def async_import_usage(self, usage_days: tuple[OgeUsageDay, ...]) -> None:
        """Import hourly usage into recorder statistics."""
        imported_hours = tuple(_build_imported_hours(self.hass, usage_days))
        if not imported_hours:
            return

        energy_base_sum = await self._async_get_sum_before(
            self.energy_statistic_id, imported_hours[0].start
        )
        cost_base_sum = await self._async_get_sum_before(
            self.cost_statistic_id, imported_hours[0].start
        )

        energy_statistics: list[StatisticData] = []
        cost_statistics: list[StatisticData] = []
        energy_sum = energy_base_sum
        cost_sum = cost_base_sum

        for imported_hour in imported_hours:
            energy_sum += float(imported_hour.kwh)
            cost_sum += float(imported_hour.cost)
            energy_statistics.append(
                StatisticData(
                    start=imported_hour.start,
                    state=float(imported_hour.kwh),
                    sum=energy_sum,
                )
            )
            cost_statistics.append(
                StatisticData(
                    start=imported_hour.start,
                    state=float(imported_hour.cost),
                    sum=cost_sum,
                )
            )

        async_add_external_statistics(
            self.hass, self.energy_metadata, energy_statistics
        )
        async_add_external_statistics(self.hass, self.cost_metadata, cost_statistics)
        _LOGGER.debug(
            "Imported %s OGE hourly rows for %s",
            len(imported_hours),
            self.account.account_number,
        )

    async def async_has_statistics(self) -> bool:
        """Return whether energy statistics already exist for the account."""
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            self.energy_statistic_id,
            True,
            {"sum"},
        )
        return bool(last_stat)

    @property
    def energy_metadata(self) -> StatisticMetaData:
        """Return metadata for imported energy statistics."""
        return StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name=f"OGE energy import {self.account.account_number}",
            source=DOMAIN,
            statistic_id=self.energy_statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )

    @property
    def cost_metadata(self) -> StatisticMetaData:
        """Return metadata for imported cost statistics."""
        return StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name=f"OGE energy cost {self.account.account_number}",
            source=DOMAIN,
            statistic_id=self.cost_statistic_id,
            unit_class=None,
            unit_of_measurement=CURRENCY_DOLLAR,
        )

    async def _async_get_sum_before(self, statistic_id: str, start: datetime) -> float:
        """Return the last cumulative sum before the given start."""
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start - timedelta(days=3660),
            start,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        records = stats.get(statistic_id, [])
        if not records:
            return 0.0

        start_timestamp = start.timestamp()
        base_sum = 0.0
        for record in records:
            record_start = record["start"]
            if record_start >= start_timestamp:
                break
            base_sum = float(record.get("sum") or 0.0)
        return base_sum


def calculate_price_per_kwh(usage_days: tuple[OgeUsageDay, ...]) -> Decimal | None:
    """Return the blended price per kWh for the imported window."""
    total_cost = Decimal("0")
    total_kwh = Decimal("0")
    for imported_hour in _build_imported_hours(None, usage_days):
        if imported_hour.kwh <= 0:
            continue
        total_cost += imported_hour.cost
        total_kwh += imported_hour.kwh

    if total_kwh <= 0:
        return None
    return total_cost / total_kwh


def _build_imported_hours(
    hass: HomeAssistant | None, usage_days: tuple[OgeUsageDay, ...]
) -> list[OgeImportedHour]:
    """Normalize usage days into hourly recorder rows."""
    if not usage_days:
        return []

    timezone = None
    if hass is not None:
        timezone = dt_util.get_time_zone(hass.config.time_zone)

    latest_day = usage_days[-1]
    latest_populated_hour = latest_day.latest_populated_hour
    imported_hours: list[OgeImportedHour] = []

    for usage_day in usage_days:
        for hour in usage_day.hours:
            if (
                usage_day.usage_date == latest_day.usage_date
                and latest_populated_hour is not None
                and not hour.has_usage
                and _hour_sort_key(hour) > _hour_sort_key(latest_populated_hour)
            ):
                continue

            imported_hours.append(
                OgeImportedHour(
                    start=_hour_start(usage_day.usage_date, hour, timezone),
                    cost=hour.cost,
                    kwh=hour.kwh,
                    peak_kw=hour.peak_kw,
                )
            )

    return imported_hours


def _hour_start(
    usage_date: date, usage_hour: OgeUsageHour, timezone: Any
) -> datetime:
    """Return the local hour start for a usage hour."""
    hour_label = int(usage_hour.hour.split(":", 1)[0])
    start_hour = hour_label - 1
    return datetime.combine(
        usage_date,
        time(hour=start_hour),
        tzinfo=timezone,
    )


def _hour_sort_key(usage_hour: OgeUsageHour) -> int:
    """Return a sortable integer for an OGE hour label."""
    return int(usage_hour.hour.split(":", 1)[0])
