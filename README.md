# pyTibber [![Build Status](https://travis-ci.org/Danielhiversen/pyTibber.svg?branch=master)](https://travis-ci.org/Danielhiversen/pyTibber)
python interace for Tibber

Go to [developer.tibber.com/](developer.tibber.com/) to get your API token

**Install **
```
pip3 install Tibber
```


**Example: **
```
import Tibber
tibber = Tibber.Tibber()
tibber.sync_update_info()
tibber.name

home=tibber.get_homes()[0]
home.sync_update_info()
home.address1

home.sync_update_current_price_info()
home.current_price_info

tibber.websession.close()
```
