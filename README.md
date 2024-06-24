# pyTibber 

[![PyPI version](https://badge.fury.io/py/pyTibber.svg)](https://badge.fury.io/py/pyTibber) 
<a href="https://github.com/ambv/black"><img alt="Code style: black" src="https://img.shields.io/badge/code%20style-black-000000.svg"></a>


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

  await home.update_price_info()
  print(home.current_price_info)
  
  # await tibber_connection.close_connection()

loop = asyncio.run(start())
```


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


