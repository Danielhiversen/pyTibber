import inspect
import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

_LOGGER = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

def log_request_query_types(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Decorator to log the name of the `document` variable."""

    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        # Get the frame of the caller
        frame = inspect.currentframe().f_back
        if not frame:
            _LOGGER.warning("Could not get caller's frame")
            return await func(*args, **kwargs)

        frame_info = inspect.getframeinfo(frame)

        # Get all local variables in the caller's scope
        local_vars = frame.f_locals

        # Find the variable name that matches the value passed as `document`
        document_value = kwargs.get("document")

        # Assuming the second argument is `document`
        if document_value is None and len(args) > 1:
            document_value = args[1]

        variable_name = None
        for var_name, var_value in local_vars.items():
            if var_value == document_value:
                variable_name = var_name
                break

        if variable_name:
            _LOGGER.debug(
                "sending request with: %s in %s", variable_name, frame_info.function,
            )

        # Execute original function
        return await func(*args, **kwargs)

    return wrapper
