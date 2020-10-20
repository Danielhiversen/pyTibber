# pyTibber 
[![Build Status](https://travis-ci.org/Danielhiversen/pyTibber.svg?branch=master)](https://travis-ci.org/Danielhiversen/pyTibber)
[![Coverage Status](https://coveralls.io/repos/github/Danielhiversen/pyTibber/badge.svg?branch=master)](https://coveralls.io/github/Danielhiversen/pyTibber?branch=master)
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
import tibber
access_token = tibber.DEMO_TOKEN
tibber_connection = tibber.Tibber(access_token)
tibber_connection.sync_update_info()
print(tibber_connection.name)

home = tibber_connection.get_homes()[0]
home.sync_update_info()
print(home.address1)

home.sync_update_price_info()

print(home.current_price_info)

tibber_connection.sync_close_connection()
```

The library is used as part of Home Assitant: [https://github.com/home-assistant/home-assistant/tree/dev/homeassistant/components/tibber](https://github.com/home-assistant/home-assistant/tree/dev/homeassistant/components/tibber)

An example of how to subscribe to realtime data (Pulse/Watty): https://github.com/home-assistant/core/blob/fca071742dd9b0827497ced9cb607f4373d0fada/homeassistant/components/tibber/sensor.py#L179
