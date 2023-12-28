"""Exceptions"""

from .const import API_ERR_CODE_UNKNOWN


class SubscriptionEndpointMissingError(Exception):
    """Exception raised when subscription endpoint is missing"""


class UserAgentMissingError(Exception):
    """Exception raised when user agent is missing"""


class HttpExceptionError(Exception):
    """Exception base for HTTP errors

    :param status: http response code
    :param message: http response message if any
    :param extension_code: http response extension if any
    """

    def __init__(
        self,
        status: int,
        message: str = "HTTP error",
        extension_code: str = API_ERR_CODE_UNKNOWN,
    ):
        self.status = status
        self.message = message
        self.extension_code = extension_code
        super().__init__(self.message)


class FatalHttpExceptionError(HttpExceptionError):
    """Exception raised for HTTP codes that are non-retriable"""


class RetryableHttpExceptionError(HttpExceptionError):
    """Exception raised for HTTP codes that are possible to retry"""


class InvalidLoginError(FatalHttpExceptionError):
    """Invalid login exception."""
