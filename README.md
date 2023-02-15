# pyTibber 

[![PyPI version](https://badge.fury.io/py/pyTibber.svg)](https://badge.fury.io/py/pyTibber) 
<a href="https://github.com/ambv/black"><img alt="Code style: black" src="https://img.shields.io/badge/code%20style-black-000000.svg"></a>
<a href="https://github.com/ambv/black/blob/master/LICENSE"><img alt="License: MIT" src="https://black.readthedocs.io/en/stable/_static/license.svg"></a>
[![Language grade: Python](https://img.shields.io/lgtm/grade/python/g/Danielhiversen/pyTibber.svg?logo=lgtm&logoWidth=18)](https://lgtm.com/projects/g/Danielhiversen/pyTibber/context:python)


Python3 library for [Tibber](https://tibber.com/).

Get electricity price and consumption.

If you use this link to signup for Tibber, you get 50 euro to buy smart home products in the Tibber store: https://invite.tibber.com/6fd7a447 

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

access_token = tibber.const.DEMO_TOKEN
tibber_connection = tibber.Tibber(access_token, user_agent="change_this")

async def home_data():
  home = tibber_connection.get_homes()[0]
  await home.fetch_consumption_data()
  await home.update_info()
  print(home.address1)

  await home.update_price_info()
  print(home.current_price_info)

async def start():
  await tibber_connection.update_info()
  print(tibber_connection.name)
  await home_data()
  await tibber_connection.close_connection()

loop = asyncio.get_event_loop()
loop.run_until_complete(start())
```


## Example realtime data:

An example of how to subscribe to realtime data (Pulse/Watty):

```python
import tibber.const
import asyncio

import aiohttp
import tibber

ACCESS_TOKEN = tibber.const.DEMO_TOKEN

def _callback(pkg):
    print(pkg)
    data = pkg.get("data")
    if data is None:
        return
    print(data.get("liveMeasurement"))


async def run():
    async with aiohttp.ClientSession() as session:
        tibber_connection = tibber.Tibber(ACCESS_TOKEN, websession=session, user_agent="change_this")
        await tibber_connection.update_info()
    home = tibber_connection.get_homes()[0]
    await home.rt_subscribe(_callback)    

    while True:
      await asyncio.sleep(10)

loop = asyncio.get_event_loop()
loop.run_until_complete(run())
```

The library is used as part of Home Assistant.


