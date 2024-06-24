"""Constants used by pyTibber"""

from http import HTTPStatus
from typing import Final

__version__ = "0.29.3"

API_ENDPOINT: Final = "https://api.tibber.com/v1-beta/gql"
DEFAULT_TIMEOUT: Final = 10
DEMO_TOKEN: Final = "5K4MVS-OjfWhK_4yrjOlFe1F6kJXPVf7eQYggo8ebAE"

RESOLUTION_HOURLY: Final = "HOURLY"
RESOLUTION_DAILY: Final = "DAILY"
RESOLUTION_WEEKLY: Final = "WEEKLY"
RESOLUTION_MONTHLY: Final = "MONTHLY"
RESOLUTION_ANNUAL: Final = "ANNUAL"

API_ERR_CODE_UNKNOWN: Final = "UNKNOWN"
API_ERR_CODE_UNAUTH: Final = "UNAUTHENTICATED"
HTTP_CODES_RETRIABLE: Final = [
    HTTPStatus.TOO_MANY_REQUESTS,
    HTTPStatus.PRECONDITION_REQUIRED,
]
HTTP_CODES_FATAL: Final = [HTTPStatus.BAD_REQUEST]
