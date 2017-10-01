# pyTibber [![Build Status](https://travis-ci.org/Danielhiversen/pyTibber.svg?branch=master)](https://travis-ci.org/Danielhiversen/pyTibber)
python interace for Tibber

Go to [developer.tibber.com/](https://developer.tibber.com/) to get your API token

## Install
```
pip3 install pyTibber
```

## Example:

```python
import Tibber
tibber = Tibber.Tibber()  # access_token=YOUR_TOKEN
tibber.sync_update_info()
print(tibber.name)

home = tibber.get_homes()[0]
home.sync_update_info()
print(home.address1)

home.sync_update_current_price_info()
print(home.current_price_info)

tibber.websession.close()
```
