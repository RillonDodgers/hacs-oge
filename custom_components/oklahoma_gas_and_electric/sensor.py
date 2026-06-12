"""Sensor platform for Oklahoma Gas & Electric."""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from homeassistant.components.sensor import (
    EntityCategory,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ACCOUNT_NUMBER, DOMAIN
from .coordinator import OgeConfigEntry, OgeData, OgeDataUpdateCoordinator

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class OgeSensorEntityDescription(SensorEntityDescription):
    """Describe an OGE sensor."""

    value_fn: Callable[[OgeData], Decimal | str | None]


ENTITY_DESCRIPTIONS: tuple[OgeSensorEntityDescription, ...] = (
    OgeSensorEntityDescription(
        key="estimated_bill",
        translation_key="estimated_bill",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="USD",
        suggested_display_precision=2,
        value_fn=lambda data: data.estimated_bill,
    ),
    OgeSensorEntityDescription(
        key="latest_hour_peak_demand",
        translation_key="latest_hour_peak_demand",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
        value_fn=lambda data: (
            _latest_populated_hour(data).peak_kw
            if _latest_populated_hour(data)
            else None
        ),
    ),
    OgeSensorEntityDescription(
        key="service_address",
        translation_key="service_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.account.service_address,
    ),
    OgeSensorEntityDescription(
        key="account_number",
        translation_key="account_number",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.account.account_number,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up OGE sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        OgeSensor(coordinator, description)
        for description in ENTITY_DESCRIPTIONS
    )


class OgeSensor(CoordinatorEntity[OgeDataUpdateCoordinator], SensorEntity):
    """Representation of an OGE sensor."""

    entity_description: OgeSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OgeDataUpdateCoordinator,
        description: OgeSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = (
            f"{coordinator.config_entry.data[CONF_ACCOUNT_NUMBER]}_{description.key}"
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        account = self.coordinator.data.account
        return DeviceInfo(
            identifiers={(DOMAIN, account.account_number)},
            manufacturer="Oklahoma Gas & Electric",
            name=_display_account_name(account.account_number),
        )

    @property
    def native_value(self) -> Decimal | str | None:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)


def _mask_account_number(account_number: str) -> str:
    """Return a masked account number suitable for UI labels."""
    digits = "".join(char for char in account_number if char.isdigit())
    suffix = digits[-4:] if len(digits) >= 4 else account_number[-4:]
    return f"XXXX{suffix}"


def _display_account_name(account_number: str) -> str:
    """Return standard user-facing account label."""
    return f"OGE {_mask_account_number(account_number)}"


def _latest_populated_hour(data: OgeData):
    """Return the latest populated hour in the most recent day."""
    if not data.usage_days:
        return None
    return data.usage_days[-1].latest_populated_hour
