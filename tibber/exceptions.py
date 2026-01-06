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
    ) -> None:
        self.status = status
        self.message = message
        self.extension_code = extension_code
        super().__init__(self.message)


class FatalHttpExceptionError(HttpExceptionError):
    """Exception raised for HTTP codes that are non-retriable"""


class RetryableHttpExceptionError(HttpExceptionError):
    """Exception raised for HTTP codes that are possible to retry"""


class RateLimitExceededError(RetryableHttpExceptionError):
    """Exception raised when rate limit is exceeded"""
    def __init__(self, status: int, message: str, extension_code: str, retry_after: int) -> None:
        super().__init__(status, message, extension_code)
        self.retry_after = retry_after


class InvalidLoginError(FatalHttpExceptionError):
    """Invalid login exception."""


class NotForDemoUserError(FatalHttpExceptionError):
    """Exception raised when trying to use a feature not available for demo users"""
