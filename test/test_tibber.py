"""Tests for pyTibber."""

import asyncio
import datetime as dt
import logging
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

import tibber
import tibber.realtime as tibber_realtime
from tibber.const import RESOLUTION_DAILY, RESOLUTION_HOURLY
from tibber.exceptions import FatalHttpExceptionError, InvalidLoginError, NotForDemoUserError
from tibber.websocket_transport import TibberWebsocketsTransport


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


class FixedDateTime(dt.datetime):
    """Controllable datetime for deterministic fetch intervals."""

    current = dt.datetime(2026, 5, 6, 2, 30, 0, tzinfo=dt.UTC)

    @classmethod
    def now(cls, tz: dt.tzinfo | None = None) -> Self:
        if tz is None:
            return cls(
                cls.current.year,
                cls.current.month,
                cls.current.day,
                cls.current.hour,
                cls.current.minute,
                cls.current.second,
                cls.current.microsecond,
            )
        return cls.fromtimestamp(cls.current.timestamp(), tz=tz)


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
                assert home.price_unit == "SEK/kWh"
                assert home.has_real_time_consumption

                assert home.current_price_total is None
                assert home.price_total == {}
                assert home.current_price_info == {}

                await home.update_current_price_info()
                assert home.current_price_total > 0
                assert isinstance(home.current_price_info.get("energy"), float | int)
                assert isinstance(home.current_price_info.get("startsAt"), str)
                assert isinstance(home.current_price_info.get("tax"), float | int)
                assert isinstance(home.current_price_info.get("total"), float | int)

                await home.update_price_info()
                for key in home.price_total:
                    assert isinstance(key, str)
                    assert isinstance(home.price_total[key], float | int)
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
async def test_tibber_current_price_rank():
    async with aiohttp.ClientSession() as session:
        tibber_connection = tibber.Tibber(
            websession=session,
            user_agent="test",
        )
        await tibber_connection.update_info()

        homes = tibber_connection.get_homes()
        assert len(homes) == 1, "No homes found"

        await homes[0].update_info_and_price_info()
        _, _, price_rank = homes[0].current_price_data()

        assert isinstance(price_rank, float), "Price rank was unset"
        assert 0 <= price_rank <= 1, "Price rank is out of range"


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
) -> None:
    """Second fetch merges old and new values through public API."""
    FixedDateTime.current = dt.datetime(2026, 5, 6, 2, 30, 0, tzinfo=dt.UTC)
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

        with patch("tibber.home.dt.datetime", FixedDateTime):
            await home.fetch_consumption_data()
            FixedDateTime.current = dt.datetime(2026, 5, 6, 4, 30, 0, tzinfo=dt.UTC)
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
) -> None:
    """Overlapping hour should be replaced, not duplicated."""
    FixedDateTime.current = dt.datetime(2026, 5, 6, 2, 15, 0, tzinfo=dt.UTC)
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

        with patch("tibber.home.dt.datetime", FixedDateTime):
            await home.fetch_consumption_data()
            FixedDateTime.current = dt.datetime(2026, 5, 6, 4, 15, 0, tzinfo=dt.UTC)
            await home.fetch_consumption_data()

        merged_by_timestamp = {entry["from"]: entry for entry in home.hourly_consumption_data}

        assert len(home.hourly_consumption_data) == 7
        assert len(merged_by_timestamp) == 7
        assert merged_by_timestamp[updated_hourly_data[0]["from"]] == updated_hourly_data[0]
        assert merged_by_timestamp[updated_hourly_data[3]["from"]] == updated_hourly_data[3]


@pytest.mark.asyncio
async def test_logging_rt_subscribe(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    async with aiohttp.ClientSession() as session:
        tibber_connection = tibber.Tibber(
            websession=session,
            user_agent="test",
        )
        await tibber_connection.update_info()
        home = tibber_connection.get_homes()[0]

        def _callback(_: dict) -> None:
            return None

        await home.rt_subscribe(_callback)
        await asyncio.sleep(1)
        home.rt_unsubscribe()
        await tibber_connection.rt_disconnect()
        await asyncio.sleep(10)


@pytest.mark.asyncio
async def test_set_access_token_updates_clients_without_realtime(monkeypatch: pytest.MonkeyPatch) -> None:
    tibber_connection = tibber.Tibber(
        websession=MagicMock(),
        user_agent="test",
    )
    reconnect = AsyncMock()
    rt_set_access_token = AsyncMock()
    data_api_set_access_token = MagicMock()

    monkeypatch.setattr(tibber_connection.realtime, "reconnect", reconnect)
    monkeypatch.setattr(tibber_connection.realtime, "set_access_token", rt_set_access_token)
    monkeypatch.setattr(tibber_connection.data_api, "set_access_token", data_api_set_access_token)

    await tibber_connection.set_access_token("new-token")

    rt_set_access_token.assert_awaited_once_with("new-token")
    data_api_set_access_token.assert_called_once_with("new-token")
    reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_access_token_delegates_realtime_reauthorization(monkeypatch: pytest.MonkeyPatch) -> None:
    tibber_connection = tibber.Tibber(
        websession=MagicMock(),
        user_agent="test",
    )
    calls: list[str] = []

    async def fake_realtime_set_access_token(_access_token: str) -> None:
        calls.append("realtime.set_access_token")

    def fake_data_api_set_access_token(_access_token: str) -> None:
        calls.append("data_api.set_access_token")

    monkeypatch.setattr(
        tibber_connection.realtime,
        "set_access_token",
        AsyncMock(side_effect=fake_realtime_set_access_token),
    )
    reconnect = AsyncMock()
    monkeypatch.setattr(tibber_connection.realtime, "reconnect", reconnect)
    monkeypatch.setattr(tibber_connection.data_api, "set_access_token", fake_data_api_set_access_token)

    await tibber_connection.set_access_token("new-token")

    assert calls == ["data_api.set_access_token", "realtime.set_access_token"]
    reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_realtime_set_access_token_reconnects_active_subscription_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, transport: TibberWebsocketsTransport) -> None:
            self.transport = transport
            self.close_async_mock = AsyncMock()
            self.close_async = self.close_async_mock

            async def mock_connect_async() -> object:
                session = object()
                self.session = session
                return session

            self.connect_async = AsyncMock(side_effect=mock_connect_async)

    monkeypatch.setattr(tibber_realtime, "Client", FakeClient)

    realtime = tibber_realtime.TibberRT("old-token", 10, "test-agent", True)
    realtime.sub_endpoint = "wss://example.test/v1-beta/gql/subscriptions"

    await realtime.connect()
    old_manager = realtime.sub_manager
    assert old_manager is not None
    assert isinstance(old_manager, FakeClient)
    assert isinstance(old_manager.transport, TibberWebsocketsTransport)
    assert old_manager.transport.init_payload["token"] == "old-token"

    await realtime.set_access_token("new-token")

    old_manager.close_async_mock.assert_awaited_once_with()
    assert realtime.session is not None
    assert realtime.sub_manager is not None
    assert realtime.sub_manager is not old_manager
    assert isinstance(realtime.sub_manager, FakeClient)
    assert isinstance(realtime.sub_manager.transport, TibberWebsocketsTransport)
    assert realtime.sub_manager.transport.init_payload["token"] == "new-token"
    realtime.sub_manager.connect_async.assert_awaited_once_with()

    await realtime.disconnect()
