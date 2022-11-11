"""Tibber API response handler"""

from http import HTTPStatus

from aiohttp import ClientResponse

from .const import (
    API_ERR_CODE_UNAUTH,
    API_ERR_CODE_UNKNOWN,
    HTTP_CODES_FATAL,
    HTTP_CODES_RETRIABLE
)
from .exceptions import FatalHttpException, InvalidLogin, RetryableHttpException


async def extract_response_data(response: ClientResponse) -> dict:
    """Extracts the response as JSON or throws a HttpException"""
    result = await response.json()

    if response.status == HTTPStatus.OK:
        return result

    if errors := result.get("errors", []):
        error_code = errors[0].get("extensions").get("code")
        error_message = errors[0].get("message")
    else:
        error_code = API_ERR_CODE_UNKNOWN

    if response.status in HTTP_CODES_RETRIABLE:
        if error_message:
            msg = error_message
        else:
            if response_body := str(response.content):
                msg = response_body
            else:
                msg = "N/A"

        raise RetryableHttpException(
            response.status, message=msg, extension_code=error_code
        )

    if response.status in HTTP_CODES_FATAL:
        if error_code == API_ERR_CODE_UNAUTH:
            msg = error_message if error_message else "failed to login"
            raise InvalidLogin(response.status, msg, error_code)

        msg = error_message if error_message else "request failed"
        raise FatalHttpException(response.status, msg, error_code)

    # if reached here the HTTP response code is unhandled
    raise FatalHttpException(
        response.status, f"Unknown error: {msg}", error_code
    )
