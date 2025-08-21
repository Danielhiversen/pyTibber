"""Tests for pyTibber."""

import asyncio
import datetime as dt
import logging

import aiohttp
import pytest

import tibber
from tibber.const import RESOLUTION_DAILY
from tibber.exceptions import FatalHttpExceptionError, InvalidLoginError, NotForDemoUserError


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

        with pytest.raises(FatalHttpExceptionError, match="Syntax Error*"):
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
async def test_tibber_token():
    async with aiohttp.ClientSession() as session:
        tibber_connection = tibber.Tibber(
            access_token="d11a43897efa4cf478afd659d6c8b7117da9e33b38232fd454b0e9f28af98012",
            websession=session,
            user_agent="test",
        )
        await tibber_connection.update_info()

        assert tibber_connection.name == "Daniel Høyer"
        assert len(tibber_connection.get_homes()) == 0
        assert len(tibber_connection.get_homes(only_active=False)) == 0


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
        _, _, _, price_rank = homes[0].current_price_data()

        assert isinstance(price_rank, int), "Price rank was unset"
        assert 1 <= price_rank <= 24, "Price rank is out of range"


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
