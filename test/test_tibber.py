"""Tests for pyTibber."""

import asyncio
import datetime as dt
import logging
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

import tibber
import tibber.realtime as tibber_realtime
from tibber.const import RESOLUTION_DAILY
from tibber.exceptions import FatalHttpExceptionError, InvalidLoginError, NotForDemoUserError
from tibber.websocket_transport import TibberWebsocketsTransport


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
    update_info = AsyncMock()
    reconnect = AsyncMock()
    rt_set_access_token = AsyncMock()
    data_api_set_access_token = MagicMock()

    monkeypatch.setattr(tibber_connection, "update_info", update_info)
    monkeypatch.setattr(tibber_connection.realtime, "reconnect", reconnect)
    monkeypatch.setattr(tibber_connection.realtime, "set_access_token", rt_set_access_token)
    monkeypatch.setattr(tibber_connection.data_api, "set_access_token", data_api_set_access_token)

    await tibber_connection.set_access_token("new-token")

    rt_set_access_token.assert_awaited_once_with("new-token")
    data_api_set_access_token.assert_called_once_with("new-token")
    update_info.assert_awaited_once_with()
    reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_access_token_reconnects_active_realtime(monkeypatch: pytest.MonkeyPatch) -> None:
    tibber_connection = tibber.Tibber(
        websession=MagicMock(),
        user_agent="test",
    )
    calls: list[str] = []

    async def fake_realtime_set_access_token(_access_token: str) -> None:
        calls.append("realtime.set_access_token")

    async def fake_update_info() -> None:
        calls.append("update_info")

    async def fake_reconnect() -> None:
        calls.append("reconnect")

    monkeypatch.setattr(
        type(tibber_connection.realtime),
        "should_restore_connection",
        property(lambda _: True),
    )
    monkeypatch.setattr(
        tibber_connection.realtime,
        "set_access_token",
        AsyncMock(side_effect=fake_realtime_set_access_token),
    )
    data_api_set_access_token = MagicMock()
    monkeypatch.setattr(tibber_connection, "update_info", AsyncMock(side_effect=fake_update_info))
    monkeypatch.setattr(tibber_connection.realtime, "reconnect", AsyncMock(side_effect=fake_reconnect))
    monkeypatch.setattr(tibber_connection.data_api, "set_access_token", data_api_set_access_token)

    await tibber_connection.set_access_token("new-token")

    data_api_set_access_token.assert_called_once_with("new-token")
    assert calls == ["realtime.set_access_token", "update_info", "reconnect"]


@pytest.mark.asyncio
async def test_realtime_set_access_token_recreates_subscription_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, transport: TibberWebsocketsTransport) -> None:
            self.transport = transport
            self.connect_async = AsyncMock(return_value=object())
            self.close_async_mock = AsyncMock()
            self.close_async = self.close_async_mock

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
    assert realtime.session is None
    assert realtime.sub_manager is None

    await realtime.connect()

    assert realtime.sub_manager is not None
    assert realtime.sub_manager is not old_manager
    assert isinstance(realtime.sub_manager.transport, TibberWebsocketsTransport)
    assert realtime.sub_manager.transport.init_payload["token"] == "new-token"
