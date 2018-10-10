# pyTibber [![Build Status](https://travis-ci.org/Danielhiversen/pyTibber.svg?branch=master)](https://travis-ci.org/Danielhiversen/pyTibber)  [![Coverage Status](https://coveralls.io/repos/github/Danielhiversen/pyTibber/badge.svg?branch=master)](https://coveralls.io/github/Danielhiversen/pyTibber?branch=master) [![PyPI version](https://badge.fury.io/py/pyTibber.svg)](https://badge.fury.io/py/pyTibber)

Python3 library for Tibber.

Read electricity price and consumption.

If you have a Tibber Pulse you can see your consumption in real time.

[Buy me a coffee :)](http://paypal.me/dahoiv)


Go to [developer.tibber.com/](https://developer.tibber.com/) to get your API token.

## Install
```
pip3 install pyTibber
```

## Example:

```python
import tibber
tibber_connection = tibber.Tibber()  # access_token=YOUR_TOKEN
tibber_connection.sync_update_info()
print(tibber_connection.name)

home = tibber_connection.get_homes()[0]
home.sync_update_info()
print(home.address1)

home.sync_update_price_info()

print(home.current_price_info)

tibber_connection.sync_close_connection()
```

The library is used as part of Home Assitant: [https://github.com/home-assistant/home-assistant/blob/dev/homeassistant/components/sensor/tibber.py](https://github.com/home-assistant/home-assistant/blob/dev/homeassistant/components/sensor/tibber.py)
