"""Constants for the Oklahoma Gas & Electric integration."""

from datetime import timedelta

DOMAIN = "oklahoma_gas_and_electric"
BASE_URL = "https://www.oge.com"
PORTAL_PATH = "/web/portal"
LOGIN_PATH = "/web/portal/home"
GET_ENERGY_ACCOUNTS_PATH = "/o/oge-services/account/get/api/GetEnergyAccounts"
GET_ESTIMATED_BILL_PATH = "/o/oge-services/account/get/api/GetEstimatedBill"
GET_ENERGY_ACCOUNT_DETAILS_PATH = (
    "/o/oge-services/account/get/api/GetEnergyAccountDetails"
)
GET_CHART_DATA_PATH = "/o/oge-services/account/post/api/GetChartData"
LOGIN_PORTLET_ID = (
    "com_oge_cpp_portlet_ORDLoginPortlet_WAR_ogeloginportlet_INSTANCE_bjbr"
)
LOGIN_ACTION = "callORDLoginService"
CONF_ACCOUNT_NUMBER = "account_number"
CONF_CORRECTION_DAYS = "correction_days"
CONF_CONTRACT_NUMBER = "contract_number"
CONF_HISTORY_DAYS = "history_days"
DEFAULT_CORRECTION_DAYS = 14
DEFAULT_HISTORY_DAYS = 90
DEFAULT_SCAN_INTERVAL = timedelta(hours=6)
MAX_CHART_REQUEST_DAYS = 30
