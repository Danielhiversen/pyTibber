"""Tibber API response handler"""

import logging
from http import HTTPStatus
from typing import Any

from aiohttp import ClientResponse

from .const import (
    API_ERR_CODE_UNAUTH,
    API_ERR_CODE_UNKNOWN,
    HTTP_CODES_FATAL,
    HTTP_CODES_RETRIABLE,
)
from .exceptions import (
    FatalHttpExceptionError,
    InvalidLoginError,
    NotForDemoUserError,
    RetryableHttpExceptionError,
)

_LOGGER = logging.getLogger(__name__)


def extract_error_details(errors: list[Any], default_message: str) -> tuple[str, str]:
    """Tries to extract the error message and code from the provided 'errors' dictionary"""
    if not errors:
        return API_ERR_CODE_UNKNOWN, default_message
    return errors[0].get("extensions").get("code"), errors[0].get("message")


async def extract_response_data(response: ClientResponse) -> dict[Any, Any]:
    """Extracts the response as JSON or throws a HttpException"""
    _LOGGER.debug("Response status: %s", response.status)

    if response.content_type != "application/json":
        raise FatalHttpExceptionError(
            response.status,
            f"Unexpected content type: {response.content_type}",
            API_ERR_CODE_UNKNOWN,
        )

    result = await response.json()

    if response.status == HTTPStatus.OK:
        return result

    if response.status in HTTP_CODES_RETRIABLE:
        error_code, error_message = extract_error_details(result.get("errors", []), str(response.content))

        raise RetryableHttpExceptionError(response.status, message=error_message, extension_code=error_code)

    if response.status in HTTP_CODES_FATAL:
        error_code, error_message = extract_error_details(result.get("errors", []), "request failed")
        if error_code == API_ERR_CODE_UNAUTH:
            raise InvalidLoginError(response.status, error_message, error_code)

        _LOGGER.error("FatalHttpExceptionError %s %s", error_message, error_code)
        raise FatalHttpExceptionError(response.status, error_message, error_code)

    error_code, error_message = extract_error_details(result.get("errors", []), "N/A")
    # if reached here the HTTP response code is not currently handled
    _LOGGER.error("FatalHttpExceptionError %s %s", error_message, error_code)
    raise FatalHttpExceptionError(response.status, f"Unhandled error: {error_message}", error_code)
