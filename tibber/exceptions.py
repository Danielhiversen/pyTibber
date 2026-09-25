"""Exceptions"""

from .const import API_ERR_CODE_UNKNOWN


class TibberError(Exception):
    """Base exception for Tibber errors."""


class SubscriptionEndpointMissingError(TibberError):
    """Exception raised when subscription endpoint is missing."""


class SubscriptionFailedError(TibberError):
    """Exception raised when subscription fails."""


class RealTimeConsumptionDisabledError(TibberError):
    """Exception raised when a home is confirmed to have real time consumption disabled.

    Passed to the `on_error` callback of `TibberHome.rt_subscribe`. This error is terminal: the
    resubscribe loop has stopped and will not retry on its own. To resume, call `rt_subscribe`
    again, from a separate task rather than directly from the callback, and rate limit the retries.
    """


class UserAgentMissingError(TibberError):
    """Exception raised when user agent is missing."""


class HttpExceptionError(TibberError):
    """Exception base for HTTP errors.

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
    """Exception raised for HTTP codes that are non-retriable."""


class RetryableHttpExceptionError(HttpExceptionError):
    """Exception raised for HTTP codes that are possible to retry."""


class RateLimitExceededError(RetryableHttpExceptionError):
    """Exception raised when rate limit is exceeded"""

    def __init__(self, status: int, message: str, extension_code: str, retry_after: float) -> None:
        super().__init__(status, message, extension_code)
        self.retry_after = retry_after


class InvalidLoginError(FatalHttpExceptionError):
    """Invalid login exception."""


class NotForDemoUserError(FatalHttpExceptionError):
    """Exception raised when trying to use a feature not available for demo users"""


class WebsocketError(TibberError):
    """Base exception for Tibber websocket errors."""


class WebsocketReconnectedError(WebsocketError):
    """Exception raised when websocket has been reconnected."""


class WebsocketTransportError(WebsocketError):
    """Exception raised when websocket transport fails."""
