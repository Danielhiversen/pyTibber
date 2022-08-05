"""
Tests for pyTibber
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../")))
import aiohttp  # noqa: E402

import tibber  # noqa: E402


@pytest.mark.asyncio
async def test_tibber_no_session():
    tibber_connection = tibber.Tibber()
    await tibber_connection.update_info()

    assert tibber_connection.name == "Arya Stark"


@pytest.mark.asyncio
async def test_tibber():
    async with aiohttp.ClientSession() as session:
        tibber_connection = tibber.Tibber(websession=session)
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

                home.sync_update_current_price_info()
                assert home.current_price_total > 0
                assert isinstance(home.current_price_info.get("energy"), (float, int))
                assert isinstance(home.current_price_info.get("startsAt"), str)
                assert isinstance(home.current_price_info.get("tax"), (float, int))
                assert isinstance(home.current_price_info.get("total"), (float, int))

                await home.update_price_info()
                for key in home.price_total.keys():
                    assert isinstance(key, str)
                    assert isinstance(home.price_total[key], (float, int))
            else:
                k += 1
                assert home.home_id == "cc83e83e-8cbf-4595-9bf7-c3cf192f7d9c"
                assert home.address1 == "Winterfell Castle 1"
                assert home.country is None

        assert k == 1

        assert len(tibber_connection.get_homes(only_active=False)) == 1
        await tibber_connection.update_info()


@pytest.mark.asyncio
async def test_tibber_invalid_token():
    async with aiohttp.ClientSession() as session:
        tibber_connection = tibber.Tibber(
            access_token="INVALID_TOKEN", websession=session
        )
        with pytest.raises(
            tibber.InvalidLogin, match="No valid access token in request"
        ):
            await tibber_connection.update_info()
        assert tibber_connection.name is None
        assert tibber_connection.get_homes() == []


@pytest.mark.asyncio
async def test_tibber_notification():
    async with aiohttp.ClientSession() as session:
        tibber_connection = tibber.Tibber(websession=session)
        await tibber_connection.update_info()
        assert not await tibber_connection.send_notification("Test tittle", "message")


@pytest.mark.asyncio
async def test_tibber_token():
    async with aiohttp.ClientSession() as session:
        tibber_connection = tibber.Tibber(
            access_token="d11a43897efa4cf478afd659d6c8b7117da9e33b38232fd454b0e9f28af98012",
            websession=session,
        )
        await tibber_connection.update_info()

        assert tibber_connection.name == "Daniel Høyer"
        assert len(tibber_connection.get_homes()) == 0
        assert len(tibber_connection.get_homes(only_active=False)) == 0
