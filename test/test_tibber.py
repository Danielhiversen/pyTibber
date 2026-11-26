"""Tests for pyTibber."""

import asyncio
import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

import tibber
from tibber.const import RESOLUTION_DAILY, RESOLUTION_HOURLY
from tibber.exceptions import FatalHttpExceptionError, InvalidLoginError, NotForDemoUserError
from tibber.live_events import LiveEventDevice

from .conftest import FixedDateTime


@pytest.fixture
def initial_hourly_data() -> list[dict]:
    """Initial dataset stored after first fetch."""
    base_time = dt.datetime(2026, 5, 6, 0, 0, 0, tzinfo=dt.UTC)
    return [
        {
            "from": (base_time - dt.timedelta(hours=5)).isoformat(),
            "to": (base_time - dt.timedelta(hours=4)).isoformat(),
            "consumption": 0.9,
            "cost": 0.45,
        },
        {
            "from": (base_time - dt.timedelta(hours=4)).isoformat(),
            "to": (base_time - dt.timedelta(hours=3)).isoformat(),
            "consumption": 1.1,
            "cost": 0.55,
        },
        {
            "from": (base_time - dt.timedelta(hours=3)).isoformat(),
            "to": (base_time - dt.timedelta(hours=2)).isoformat(),
            "consumption": 1.3,
            "cost": 0.65,
        },
        {
            "from": (base_time - dt.timedelta(hours=2)).isoformat(),
            "to": (base_time - dt.timedelta(hours=1)).isoformat(),
            "consumption": 1.0,
            "cost": 0.5,
        },
        {
            "from": (base_time - dt.timedelta(hours=1)).isoformat(),
            "to": base_time.isoformat(),
            "consumption": 1.5,
            "cost": 0.75,
        },
    ]


@pytest.fixture
def updated_hourly_data() -> list[dict]:
    """Second dataset with unchanged, corrected and new timestamps."""
    base_time = dt.datetime(2026, 5, 6, 0, 0, 0, tzinfo=dt.UTC)
    return [
        {
            "from": (base_time - dt.timedelta(hours=4)).isoformat(),
            "to": (base_time - dt.timedelta(hours=3)).isoformat(),
            "consumption": 1.1,
            "cost": 0.55,
        },
        {
            "from": (base_time - dt.timedelta(hours=3)).isoformat(),
            "to": (base_time - dt.timedelta(hours=2)).isoformat(),
            "consumption": 1.7,
            "cost": 0.85,
        },
        {
            "from": (base_time - dt.timedelta(hours=2)).isoformat(),
            "to": (base_time - dt.timedelta(hours=1)).isoformat(),
            "consumption": 1.0,
            "cost": 0.5,
        },
        {
            "from": (base_time - dt.timedelta(hours=1)).isoformat(),
            "to": base_time.isoformat(),
            "consumption": 2.2,
            "cost": 1.1,
        },
        {
            "from": base_time.isoformat(),
            "to": (base_time + dt.timedelta(hours=1)).isoformat(),
            "consumption": 1.2,
            "cost": 0.6,
        },
        {
            "from": (base_time + dt.timedelta(hours=1)).isoformat(),
            "to": (base_time + dt.timedelta(hours=2)).isoformat(),
            "consumption": 1.4,
            "cost": 0.7,
        },
    ]


@pytest.mark.asyncio
async def test_tibber_no_session():
    tibber_connection = tibber.Tibber(
        user_agent="test",
    )
    await tibber_connection.update_info()

    assert tibber_connection.name == "Arya Stark"


@pytest.mark.asyncio
async def test_tibber():
    async with aiohttp.ClientSession() as session:
        tibber_connection = tibber.Tibber(
            websession=session,
            user_agent="test",
        )
        await tibber_connection.update_info()

        assert tibber_connection.name == "Arya Stark"
        assert len(tibber_connection.get_homes(only_active=False)) == 1

        assert tibber_connection.get_home("INVALID_KEY") is None

        k = 0
        for home in tibber_connection.get_homes(only_active=False):
            await home.update_info()
            if home.home_id == "c70dcbe5-4485-4821-933d-a8a86452737b":
                k += 1
                assert home.home_id == "c70dcbe5-4485-4821-933d-a8a86452737b"
                assert home.address1 == "Kungsgatan 8"
                assert home.country == "SE"

            else:
                k += 1
                assert home.home_id == "96a14971-525a-4420-aae9-e5aedaa129ff"
                assert home.address1 == "Winterfell Castle 1"
                assert home.country is None

        assert k == 1

        assert len(tibber_connection.get_homes(only_active=False)) == 1
        await tibber_connection.update_info()


@pytest.mark.asyncio
async def test_tibber_invalid_token():
    async with aiohttp.ClientSession() as session:
        tibber_connection = tibber.Tibber(
            access_token="INVALID_TOKEN",
            websession=session,
            user_agent="test",
        )
        with pytest.raises(InvalidLoginError, match="invalid token"):
            await tibber_connection.update_info()
        assert not tibber_connection.name
        assert tibber_connection.get_homes() == []


@pytest.mark.asyncio
async def test_tibber_invalid_query():
    async with aiohttp.ClientSession() as session:
        tibber_connection = tibber.Tibber(
            websession=session,
            user_agent="test",
        )

        with pytest.raises(FatalHttpExceptionError, match=r"Syntax Error*"):
            await tibber_connection.execute("invalidquery")

        assert not tibber_connection.name
        assert tibber_connection.get_homes() == []


@pytest.mark.asyncio
async def test_tibber_notification():
    async with aiohttp.ClientSession() as session:
        tibber_connection = tibber.Tibber(
            websession=session,
            user_agent="test",
        )
        with pytest.raises(NotForDemoUserError, match="operation not allowed for demo user"):
            await tibber_connection.send_notification("Test title", "message")


@pytest.mark.asyncio
async def test_tibber_get_historic_data():
    async with aiohttp.ClientSession() as session:
        tibber_connection = tibber.Tibber(
            websession=session,
            user_agent="test",
        )
        await tibber_connection.update_info()

        homes = tibber_connection.get_homes()
        assert len(homes) == 1, f"Expected 1 home, got '{len(homes)}'"

        home = homes[0]
        assert home is not None

        historic_data = await home.get_historic_data_date(dt.datetime(2024, 1, 1, tzinfo=dt.UTC), 5, RESOLUTION_DAILY)
        assert len(historic_data) == 5
        assert historic_data[0]["from"] == "2024-01-01T00:00:00.000+01:00", "First day must be 2024-01-01"
        assert historic_data[4]["from"] == "2024-01-05T00:00:00.000+01:00", "Last day must be 2024-01-05"


@pytest.mark.asyncio
async def test_fetch_consumption_data_merges_using_two_predefined_payloads(
    monkeypatch: pytest.MonkeyPatch,
    initial_hourly_data: list[dict],
    updated_hourly_data: list[dict],
    frozen_clock: type[FixedDateTime],
) -> None:
    """Second fetch merges old and new values through public API."""
    frozen_clock.current = dt.datetime(2026, 5, 6, 2, 30, 0, tzinfo=dt.UTC)
    payloads = iter([initial_hourly_data, updated_hourly_data])

    async def mock_get_historic_data(
        _n_hours: int,
        resolution: str = RESOLUTION_HOURLY,
        production: bool = False,
    ) -> list[dict]:
        _ = (resolution, production)
        return next(payloads)

    async with aiohttp.ClientSession() as session:
        tibber_connection = tibber.Tibber(websession=session, user_agent="test")
        await tibber_connection.update_info()
        home = tibber_connection.get_homes()[0]

        monkeypatch.setattr(home, "get_historic_data", mock_get_historic_data)

        with patch("tibber.home.dt.datetime", frozen_clock):
            await home.fetch_consumption_data()
            frozen_clock.current = dt.datetime(2026, 5, 6, 4, 30, 0, tzinfo=dt.UTC)
            await home.fetch_consumption_data()

        assert home.hourly_consumption_data == [
            initial_hourly_data[0],
            updated_hourly_data[0],
            updated_hourly_data[1],
            updated_hourly_data[2],
            updated_hourly_data[3],
            updated_hourly_data[4],
            updated_hourly_data[5],
        ]


@pytest.mark.asyncio
async def test_fetch_consumption_data_does_not_duplicate_overlapping_timestamp(
    monkeypatch: pytest.MonkeyPatch,
    initial_hourly_data: list[dict],
    updated_hourly_data: list[dict],
    frozen_clock: type[FixedDateTime],
) -> None:
    """Overlapping hour should be replaced, not duplicated."""
    frozen_clock.current = dt.datetime(2026, 5, 6, 2, 15, 0, tzinfo=dt.UTC)
    payloads = iter([initial_hourly_data, updated_hourly_data])

    async def mock_get_historic_data(
        _n_hours: int,
        resolution: str = RESOLUTION_HOURLY,
        production: bool = False,
    ) -> list[dict]:
        _ = (resolution, production)
        return next(payloads)

    async with aiohttp.ClientSession() as session:
        tibber_connection = tibber.Tibber(websession=session, user_agent="test")
        await tibber_connection.update_info()
        home = tibber_connection.get_homes()[0]

        monkeypatch.setattr(home, "get_historic_data", mock_get_historic_data)

        with patch("tibber.home.dt.datetime", frozen_clock):
            await home.fetch_consumption_data()
            frozen_clock.current = dt.datetime(2026, 5, 6, 4, 15, 0, tzinfo=dt.UTC)
            await home.fetch_consumption_data()

        merged_by_timestamp = {entry["from"]: entry for entry in home.hourly_consumption_data}

        assert len(home.hourly_consumption_data) == 7
        assert len(merged_by_timestamp) == 7
        assert merged_by_timestamp[updated_hourly_data[0]["from"]] == updated_hourly_data[0]
        assert merged_by_timestamp[updated_hourly_data[3]["from"]] == updated_hourly_data[3]


@pytest.mark.asyncio
async def test_set_access_token_emits_deprecation_warning() -> None:
    """set_access_token must emit a DeprecationWarning."""
    tibber_connection = tibber.Tibber(
        access_token="existing-token",
        websession=MagicMock(),
        user_agent="test",
    )

    with pytest.warns(DeprecationWarning, match="refresh_access_token"):
        await tibber_connection.set_access_token("new-token")


@pytest.mark.asyncio
@pytest.mark.parametrize("access_token", ["new-token", "existing-token"])
async def test_set_access_token_updates_token_manager(access_token: str) -> None:
    """set_access_token must update the shared TokenManager."""
    tibber_connection = tibber.Tibber(
        access_token="existing-token",
        websession=MagicMock(),
        user_agent="test",
    )

    with pytest.warns(DeprecationWarning, match="refresh_access_token"):
        await tibber_connection.set_access_token(access_token)

    assert tibber_connection._token_manager.access_token == access_token  # noqa: SLF001


@pytest.mark.asyncio
async def test_rt_disconnect_cancels_home_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """rt_disconnect cancels the live task of every subscribed home and waits until they have ended."""
    tibber_connection = tibber.Tibber(
        access_token="existing-token",
        websession=MagicMock(),
        user_agent="test",
    )
    tibber_connection._all_home_ids = ["a", "b", "c"]  # noqa: SLF001

    discovering: list[str] = []

    async def discover_forever(home_id: str) -> list[LiveEventDevice]:
        discovering.append(home_id)
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(tibber_connection.data_api, "get_live_event_devices", discover_forever)
    subscribed = [tibber_connection.get_home("a"), tibber_connection.get_home("b")]
    tibber_connection.get_home("c")  # never subscribed
    for home in subscribed:
        await home.rt_subscribe(MagicMock())
    tasks = [home._rt_task for home in subscribed]  # noqa: SLF001
    await asyncio.sleep(0)
    assert discovering == ["a", "b"]

    await tibber_connection.rt_disconnect()

    assert all(task.cancelled() for task in tasks)


@pytest.mark.asyncio
async def test_close_connection_stops_live_tasks_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """close_connection ends the live tasks before it closes the websession, so they can't reconnect on it."""
    websession = MagicMock()
    tibber_connection = tibber.Tibber(access_token="existing-token", websession=websession, user_agent="test")
    tibber_connection._all_home_ids = ["a"]  # noqa: SLF001

    async def discover_forever(_home_id: str) -> list[LiveEventDevice]:
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(tibber_connection.data_api, "get_live_event_devices", discover_forever)
    home = tibber_connection.get_home("a")
    await home.rt_subscribe(MagicMock())
    task = home._rt_task  # noqa: SLF001
    await asyncio.sleep(0)
    task_done_at_close: list[bool] = []
    websession.close = AsyncMock(side_effect=lambda: task_done_at_close.append(task.done()))

    await tibber_connection.close_connection()

    assert task_done_at_close == [True]
    assert task.cancelled()
