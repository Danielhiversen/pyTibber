"""Exceptions"""

from .const import API_ERR_UNAUTH


class HttpException(Exception):
    """Exception base for HTTP errors

    :param status: http response code
    :param message: http response message if any
    :param extension_code: http response extension if any
    """

    def __init__(
        self, status: int, message: str = "HTTP error", extension_code: str = ""
    ):
        self.status = status
        self.message = message
        self.extension_code = extension_code
        super().__init__(self.message)


class InvalidLogin(HttpException):
    """Invalid login exception."""

    def __init__(self, message: str = "failed to login"):
        self.message = message
        super().__init__(400, self.message, API_ERR_UNAUTH)


class FatalHttpException(HttpException):
    """Exception raised for HTTP codes that are non-retriable

    :param status: http response code
    :param message: http response message if any
    :param extension_code: http response extension if any
    """


class RetryableHttpException(HttpException):
    """Exception raised for HTTP codes that are possible to retry

    :param status: http response code
    :param retry_after_sec: indicate to the user that the request can be retried after X seconds
    :param message: http response message if any
    :param extension_code: http response extension if any
    """

    def __init__(
        self,
        status: int,
        retry_after_sec: int = 0,
        message: str = "",
        extension_code: str = "",
    ):
        self.retry_after_sec = retry_after_sec
        super().__init__(status, message, extension_code)
