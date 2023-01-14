"""Tibber API response handler"""

from http import HTTPStatus

from aiohttp import ClientResponse

from .const import (
    API_ERR_CODE_UNAUTH,
    API_ERR_CODE_UNKNOWN,
    HTTP_CODES_FATAL,
    HTTP_CODES_RETRIABLE,
)
from .exceptions import FatalHttpException, InvalidLogin, RetryableHttpException


def extract_error_details(errors: dict, default_message: str = "") -> tuple[str, str]:
    """Tries to extract the error message and code from the provided 'errors' dictionary"""
    error_code = API_ERR_CODE_UNKNOWN
    if default_message:
        error_message = default_message
    else:
        error_message = "N/A"

    if errors:
        error_code = errors[0].get("extensions").get("code")
        error_message = errors[0].get("message")

    return error_code, error_message


async def extract_response_data(response: ClientResponse) -> dict:
    """Extracts the response as JSON or throws a HttpException"""
    result = await response.json()

    if response.status == HTTPStatus.OK:
        return result

    if response.status in HTTP_CODES_RETRIABLE:
        error_code, error_message = extract_error_details(
            result.get("errors", []), str(response.content)
        )

        raise RetryableHttpException(
            response.status, message=error_message, extension_code=error_code
        )

    if response.status in HTTP_CODES_FATAL:
        error_code, error_message = extract_error_details(
            result.get("errors", []), "request failed"
        )
        if error_code == API_ERR_CODE_UNAUTH:
            raise InvalidLogin(response.status, error_message, error_code)

        raise FatalHttpException(response.status, error_message, error_code)

    error_code, error_message = extract_error_details(result.get("errors", []))
    # if reached here the HTTP response code is not currently handled
    raise FatalHttpException(
        response.status, f"Unhandled error: {error_message}", error_code
    )
