# pyTibber

[![PyPI version](https://badge.fury.io/py/pyTibber.svg)](https://badge.fury.io/py/pyTibber)
<a href="https://github.com/astral-sh/ruff">
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff">
</a>
<a href="https://github.com/pre-commit/pre-commit">
  <img src="https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white&style=flat-square" alt="pre-commit">
</a>


Python3 library for [Tibber](https://tibber.com/).

Get electricity price and consumption.

If you have a Tibber Pulse or Watty you can see your consumption in real time.

[Buy me a coffee :)](http://paypal.me/dahoiv)


Go to [developer.tibber.com/](https://developer.tibber.com/) to get your API token.

## Install
```
pip3 install pyTibber
```

## Example:

```python
import tibber.const
import tibber
import asyncio


async def start():
  tibber_connection = tibber.Tibber(tibber.const.DEMO_TOKEN, user_agent="change_this")
  await tibber_connection.update_info()
  print(tibber_connection.name)

  home = tibber_connection.get_homes()[0]
  await home.fetch_consumption_data()
  await home.update_info()
  print(home.address1)

  # Prices come from the Tibber Data API, which needs an OAuth access token with the
  # data-api-prices-read scope (https://data-api.tibber.com/docs/prices/), not the demo token.
  # await home.update_prices()  # today and, from about 13:00 CET, tomorrow
  # print(home.current_price_data())  # (price, start time, rank among today's prices)
  # print(home.current_price_interval)  # consumption and production prices, incl. subsidized price
  # await home.get_historic_price_data(start, end, tibber.const.RESOLUTION_DAILY)  # mean price per day

  # await tibber_connection.close_connection()

loop = asyncio.run(start())
```

## Prices and upgrading to 0.40

Prices come from the [Tibber Data API](https://data-api.tibber.com/docs/prices/) instead of GraphQL.
This breaks code that used the GraphQL prices:

- You need an OAuth access token with the `data-api-prices-read` scope. A personal GraphQL token or the demo token
  makes `update_prices()` raise `InvalidLoginError`, and a token without the scope `InsufficientScopeError`.
  A home that has no prices raises nothing, it just has no `prices`.
- `update_info()` no longer fetches prices. Call `update_prices()`. Until you do, there is no error:
  `current_price_data()` returns `(None, None, None)` and `current_attributes()` returns `{}`.
- Removed: `update_info_and_price_info()`, `price_total` and `current_price_rank()`. Use `prices`, `prices_for_day()`,
  `current_price_interval` and `current_price_data()` instead.
- The prices are 15-minute `PriceInterval`s with `PriceDetails` for consumption and production: `price`,
  `subsidized_price`, `third_party_price` and `vat_percent`. The GraphQL `level`, `energy` and `tax` are gone.
- `get_historic_price_data(start, end, resolution)` takes a time range now, and averages the 15-minute prices per hour,
  day or month. Weekly and annual resolutions are not supported.
- `currency` is the currency of the prices, or until they are fetched the currency of the home's country.
- `current_attributes()` returns `{}` instead of zeros when there are no prices today.


## Example realtime data:

An example of how to subscribe to realtime data (Pulse/Watty):

```python
import tibber.const
import asyncio

import aiohttp
import tibber

def _callback(pkg):
    print(pkg)
    data = pkg.get("data")
    if data is None:
        return
    print(data.get("liveMeasurement"))


async def run():
    async with aiohttp.ClientSession() as session:
        tibber_connection = tibber.Tibber(tibber.const.DEMO_TOKEN, websession=session, user_agent="change_this")
        await tibber_connection.update_info()
    home = tibber_connection.get_homes()[0]
    await home.rt_subscribe(_callback)

    while True:
      await asyncio.sleep(10)

loop = asyncio.run(run())
```

The library is used as part of Home Assistant.


