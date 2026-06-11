# AGENTS.md

## Project

This repository contains a HACS-compatible Home Assistant custom integration for **Oklahoma Gas & Electric**.

Repository layout:

- `custom_components/oklahoma_gas_and_electric/`
- `README.md`
- `hacs.json`

There is only one integration in this repository. Do not add a second integration under `custom_components/`.

## Goal

The integration provides OGE electric usage data to Home Assistant, with a strong focus on **Energy Dashboard compatibility**.

The intended architecture is:

- authenticate against the OGE portal
- fetch hourly usage and hourly cost data from OGE
- import that data into Home Assistant recorder as **external statistics**
- use those imported statistics as the Energy Dashboard source of truth

Do not redesign this around a fake lifetime meter or a separate custom database.

## Important product decisions

### Energy Dashboard model

Use Home Assistant recorder external statistics, not:

- a fabricated cumulative meter sensor
- `utility_meter` as the primary source architecture
- a custom SQLite table inside the integration

OGE only provides interval usage data. The correct dashboard path is imported recorder statistics.

### Stored history

Historical usage should live in Home Assistant's recorder database, not in a custom integration database.

The integration should:

- backfill historical data from OGE on first load
- re-fetch a rolling correction window
- re-import the same timestamps safely so corrected utility data can overwrite prior rows

### Current exposed entities

Keep the entity surface intentionally small:

- `estimated_bill`
- `price_per_kwh`
- `latest_hour_peak_demand`
- `service_address`
- `account_number`

Do not reintroduce the old broad helper-sensor set unless there is a clear user-facing reason.

Previously exposed sensors like `today_energy`, `today_cost`, `yesterday_*`, and `latest_hour_energy` were intentionally removed because they were UI noise once recorder statistics were in place.

### `price_per_kwh`

`price_per_kwh` is a helper sensor only.

It represents a blended effective rate over the fetched usage window:

- `sum(cost) / sum(kWh)`

It is not:

- an official tariff schedule
- a billing-cycle-specific rate plan
- an Energy Dashboard cost source

## OGE API notes

### Auth flow

The working login flow is not a simple bare POST.

Current expected flow:

1. `GET /web/portal`
2. extract `p_auth` from the returned HTML
3. `POST /web/portal/home` with:
   - `p_p_id=com_oge_cpp_portlet_ORDLoginPortlet_WAR_ogeloginportlet_INSTANCE_bjbr`
   - `p_p_lifecycle=1`
   - `_com_oge_cpp_portlet_ORDLoginPortlet_WAR_ogeloginportlet_INSTANCE_bjbr_javax.portlet.action=callORDLoginService`
   - `p_auth=<token>`
   - form fields `userName` and `password`

Do not simplify this flow unless it is re-verified against the live site.

### Important endpoint behavior

- `GetEnergyAccounts` returns account objects using `accountId`
- nested payload fields are often JSON strings inside outer JSON
- chart data is the primary source for hourly usage/cost
- chart response includes hourly `kwh`, hourly `cost`, and quarter-hour `kw` readings

### Usage data semantics

`GetChartData` returns:

- daily totals
- hourly buckets
- quarter-hour demand readings inside each hour

The integration currently uses:

- hourly `kwh` for imported energy statistics
- hourly `cost` for imported cost statistics
- `max(kw)` within an hour for peak-demand helper calculations

Trailing zero rows on the most recent day may be placeholders for future hours and should not be blindly treated as actual usage.

## Current implementation expectations

### Statistics

The integration should continue to import:

- energy statistic id:
  - `oklahoma_gas_and_electric:<account>_energy_import`
- cost statistic id:
  - `oklahoma_gas_and_electric:<account>_energy_cost`

These are the intended Energy Dashboard sources.

### Options

Current expected options:

- `history_days` default `90`
- `correction_days` default `14`

Refresh should prefer a correction-window model after first import.

### Polling

Current intended polling interval is every 6 hours.

## Files of interest

Primary runtime files:

- `custom_components/oklahoma_gas_and_electric/__init__.py`
- `custom_components/oklahoma_gas_and_electric/api.py`
- `custom_components/oklahoma_gas_and_electric/config_flow.py`
- `custom_components/oklahoma_gas_and_electric/const.py`
- `custom_components/oklahoma_gas_and_electric/coordinator.py`
- `custom_components/oklahoma_gas_and_electric/sensor.py`
- `custom_components/oklahoma_gas_and_electric/statistics.py`
- `custom_components/oklahoma_gas_and_electric/manifest.json`
- `custom_components/oklahoma_gas_and_electric/strings.json`
- `custom_components/oklahoma_gas_and_electric/translations/en.json`

## Development guidance

- Keep this repository HACS-compatible.
- Keep all runtime files inside `custom_components/oklahoma_gas_and_electric/`.
- Do not add tests to this repository unless explicitly requested.
- Prefer small, first-party-feeling Home Assistant patterns over custom persistence or workarounds.
- If changing auth behavior, recorder-stat import behavior, or dashboard compatibility, preserve the architectural intent described above.

## If future work expands

Reasonable future additions:

- better README/docs
- release tagging/versioning
- recorder-statistics migration handling
- more explicit dashboard setup documentation
- additional helper sensors only if they provide clear value without duplicating dashboard/history views
