# CLAUDE.md — pyTibber Developer Guide for AI Assistants

## Project Overview

**pyTibber** is an async Python library for the [Tibber](https://tibber.com) electricity API. It provides:
- GraphQL API client for electricity pricing and consumption data
- Real-time data streaming via WebSocket subscriptions (Tibber Pulse/Watty devices)
- REST Data API client for device and sensor management

The library is used as a foundation by **Home Assistant** and other applications needing to integrate Tibber electricity data.

- **Current version:** `0.36.0` (defined in `tibber/const.py`)
- **Python requirement:** `>= 3.11`
- **License:** GPL-3.0-or-later

---

## Repository Layout

```
pyTibber/
├── tibber/                    # Main source package
│   ├── __init__.py            # Tibber class — primary entry point
│   ├── const.py               # Constants, version, API URLs, resolution types
│   ├── data_api.py            # TibberDataAPI REST client (devices, sensors)
│   ├── exceptions.py          # Custom exception hierarchy
│   ├── gql_queries.py         # GraphQL query string definitions
│   ├── home.py                # TibberHome and HourlyData classes
│   ├── realtime.py            # TibberRT — WebSocket real-time manager
│   ├── response_handler.py    # HTTP/GraphQL response processing
│   └── websocket_transport.py # Custom WebSocket transport (extends gql)
├── test/
│   ├── test_tibber.py         # Integration tests (against demo account)
│   └── test_data_api.py       # Unit tests for Data API (mocked)
├── .github/workflows/
│   ├── code_checker.yml       # CI: lint + test on Python 3.12 & 3.13
│   ├── python-publish.yml     # CI: PyPI release on GitHub release creation
│   ├── codeql.yml             # Security scanning
│   └── rebase.yml             # Auto-rebase workflow
├── .pre-commit-config.yaml    # Pre-commit hooks (ruff, mypy, whitespace)
├── .coveragerc                # pytest-cov configuration
├── pyproject.toml             # Project metadata, dependencies, ruff/mypy config
└── setup.cfg                  # Flake8 configuration (max-line-length=120)
```

---

## Architecture and Key Classes

### Class Hierarchy

```
Tibber                          # Main entry point, one per user
├── TibberDataAPI               # REST API: devices & sensors
├── TibberRT                    # WebSocket real-time manager
└── list[TibberHome]            # One per registered home

TibberHome                      # Represents a single Tibber home
├── HourlyData                  # Holds consumption/production data
└── Methods for pricing, history, and real-time subscription

TibberRT                        # Manages WebSocket lifecycle
└── TibberWebsocketsTransport   # Extends gql's WebsocketsTransport

TibberDataAPI                   # REST API client
├── list[TibberDevice]
└── TibberDevice
    └── list[Sensor]
```

### Module Responsibilities

| Module | Purpose |
|---|---|
| `tibber/__init__.py` | `Tibber` class: GraphQL execution, home management, notifications |
| `tibber/home.py` | `TibberHome`: pricing, consumption, production, real-time subscription |
| `tibber/realtime.py` | `TibberRT`: WebSocket lifecycle, reconnection, watchdog |
| `tibber/data_api.py` | `TibberDataAPI`: REST endpoints, rate limiting, device management |
| `tibber/gql_queries.py` | GraphQL query string templates (parameterized) |
| `tibber/response_handler.py` | Extracts data/errors from HTTP and GraphQL responses |
| `tibber/exceptions.py` | Exception hierarchy with status code and extension code support |
| `tibber/const.py` | API URLs, `__version__`, resolution constants, HTTP status code sets |
| `tibber/websocket_transport.py` | Timeout-protected WebSocket transport |

---

## Development Setup

### Install Dependencies

```bash
pip install -e ".[dev]"
# or install tools individually:
pip install aiohttp "gql>=4.0.0" "websockets>=14.0.0"
pip install mypy pytest pytest-asyncio pytest-cov ruff pre-commit
```

### Enable Pre-Commit Hooks

```bash
pre-commit install
```

Hooks run automatically on `git commit`:
- Trailing whitespace removal
- `ruff` linting (auto-fix enabled)
- `ruff format` (auto-fix enabled)
- `mypy` type checking (on `tibber/` and `test/` files)

---

## Running Tests

```bash
# Run all tests
pytest

# With coverage report
pytest --cov=tibber

# Run a specific test file
pytest test/test_tibber.py
pytest test/test_data_api.py
```

**Test types:**
- `test/test_tibber.py` — Integration tests against the **live Tibber demo account** (uses `DEMO_TOKEN` from `tibber/const.py`)
- `test/test_data_api.py` — Unit tests with `AsyncMock`/`MagicMock` for the Data API

### Async Test Convention

All async tests use `pytest-asyncio`. Mark them with:

```python
@pytest.mark.asyncio
async def test_something():
    ...
```

---

## Code Style and Quality

### Formatting and Linting: Ruff

Configuration is in `pyproject.toml`:
- **Line length:** 120 characters
- **Target version:** Python 3.11
- **Rules:** `ALL` selected; specific rules ignored for test files (e.g., `S101` for asserts)

```bash
ruff check --fix tibber/ test/
ruff format tibber/ test/
```

### Type Checking: Mypy (strict)

```bash
mypy tibber test
```

Mypy is configured with `strict = true` in `pyproject.toml`. All functions must have type annotations. Use `TYPE_CHECKING` blocks to avoid circular imports.

### Key Style Conventions

- Use `|` union syntax instead of `Optional[X]` or `Union[X, Y]` (Python 3.10+ style)
- Module-level loggers: `_LOGGER = logging.getLogger(__name__)`
- Use `@property` decorators for computed/lazy attributes
- Use `contextlib.suppress()` for expected exceptions
- All public API methods must be `async`

---

## Error Handling

### Exception Hierarchy

```
HttpExceptionError
├── FatalHttpExceptionError       # Do not retry (4xx except 429)
│   ├── InvalidLoginError         # 401
│   └── NotForDemoUserError       # Blocked for demo accounts
└── RetryableHttpExceptionError   # Retry (5xx, 429, network errors)
    └── RateLimitExceededError    # 429 Too Many Requests
```

HTTP status codes are categorized in `tibber/const.py`:
- `FATAL_HTTP_CODES`: non-retriable status codes
- `RETRIABLE_HTTP_CODES`: status codes that warrant retry

### Retry / Rate Limiting Pattern

`TibberDataAPI` implements rate limiting with exponential backoff. When adding new API calls that may be rate-limited, follow the existing pattern in `data_api.py`.

---

## GraphQL Queries

All query strings live in `tibber/gql_queries.py`. They are formatted strings supporting parameterization. When adding new queries:
1. Define the query string as a module-level constant or function in `gql_queries.py`
2. Execute via `Tibber.execute()` which handles authentication, error extraction, and retries

---

## Real-Time Subscriptions

Real-time data (live power usage) uses the GraphQL subscription protocol over WebSockets:

1. `TibberHome.rt_subscribe(callback)` — subscribes a home to real-time updates
2. `TibberRT` — manages the shared WebSocket connection and watchdog
3. `TibberWebsocketsTransport` — low-level WebSocket handling with timeouts

**Important:** The WebSocket token is refreshed on each reconnection to avoid using stale tokens (fixed in recent commits). See `realtime.py` for the reconnection logic.

---

## Data API (REST)

`TibberDataAPI` wraps Tibber's REST Data API:
- Endpoint defined in `tibber/const.py`
- Handles `TibberDevice` and `Sensor` objects
- Supports `get_homes()`, `get_all_devices()`, `get_device()`, `update_devices()`, `get_userinfo()`
- Rate limiting built in

---

## Release and Versioning

1. Update version in `tibber/const.py`: `__version__ = "X.Y.Z"`
2. Commit and push to `master`
3. Create a GitHub release — this triggers `python-publish.yml` to publish to PyPI automatically

Version is read dynamically from `tibber.const.__version__` via `pyproject.toml`.

---

## CI/CD

| Workflow | Trigger | Actions |
|---|---|---|
| `code_checker.yml` | push, PR, daily 4 AM UTC | pre-commit, pytest on Python 3.12 & 3.13 |
| `python-publish.yml` | GitHub release | Build and publish to PyPI |
| `codeql.yml` | push, PR, schedule | Security analysis |
| `rebase.yml` | PR events | Auto-rebase support |

---

## Key Constants (tibber/const.py)

```python
__version__ = "0.36.0"
DEMO_TOKEN = "5K4MVS-OjfWhK_4yrjOlFe1F6kJXPVf7eQYggo8ebAE"  # For tests only

# Data resolution types for historical data
RESOLUTION_HOURLY = "HOURLY"
RESOLUTION_DAILY = "DAILY"
RESOLUTION_WEEKLY = "WEEKLY"
RESOLUTION_MONTHLY = "MONTHLY"
RESOLUTION_ANNUAL = "ANNUAL"
```

---

## Common Development Tasks

### Add a New GraphQL Query
1. Add query string to `tibber/gql_queries.py`
2. Add method to appropriate class (`Tibber`, `TibberHome`)
3. Call `self._tibber.execute(query)` or `self.execute(query)` with error handling
4. Add type annotations; run `mypy tibber`
5. Add tests in `test/test_tibber.py`

### Add a New Data API Endpoint
1. Add method to `TibberDataAPI` in `tibber/data_api.py`
2. Follow existing rate-limit and retry patterns
3. Add unit tests with mocking in `test/test_data_api.py`

### Add a New Exception Type
1. Add to `tibber/exceptions.py` extending the appropriate base class
2. Raise it in `response_handler.py` or the relevant module
3. Document which HTTP status codes or error codes trigger it

### Modify Real-Time Subscription Logic
- Core logic: `tibber/realtime.py` (`TibberRT`)
- Transport layer: `tibber/websocket_transport.py`
- Home-level subscription entry point: `tibber/home.py` (`TibberHome.rt_subscribe`)
- Test: `test/test_tibber.py` (`test_logging_rt_subscribe`)

---

## Notes for AI Assistants

- **Always run pre-commit or ruff + mypy** before considering code changes complete.
- **Do not use `Optional[X]`** — use `X | None` instead (project style).
- **All public methods should be async** — this is an async-first library.
- **Test against the demo account** — `DEMO_TOKEN` in `const.py` is a public demo token for integration tests.
- **Do not modify `__version__`** unless explicitly performing a release bump.
- **Line length is 120**, not the default 88 — configure your tools accordingly.
- The project targets **Python 3.11+** — use modern Python features (match statements, `|` unions, etc.) where appropriate.
