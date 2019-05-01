# -*- coding: utf-8 -*-
"""
Tests for pyTibber
"""
import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../')))
import aiohttp
import tibber


class TestTibber(unittest.TestCase):
    """
    Tests Tibber
    """

    def setUp(self):     # pylint: disable=invalid-name
        """ things to be run when tests are started. """
        self.tibber = tibber.Tibber()
        self.tibber.sync_update_info()

    def tearDown(self):  # pylint: disable=invalid-name
        """ Stop stuff we started. """
        self.tibber.sync_close_connection()

    def test_tibber(self):
        self.assertEqual(self.tibber.name, 'Arya Stark')
        self.assertEqual(len(self.tibber.get_homes()), 1)

    def test_invalid_home(self):
        home = self.tibber.get_home("INVALID_KEY")
        self.assertEqual(home, None)

    def test_home(self):
        k = 0
        for home in self.tibber.get_homes(only_active=False):
            home.sync_update_info()
            if home.home_id == 'c70dcbe5-4485-4821-933d-a8a86452737b':
                k += 1
                self.assertEqual(home.home_id, 'c70dcbe5-4485-4821-933d-a8a86452737b')
                self.assertEqual(home.address1, 'Kungsgatan 8')
                self.assertEqual(home.country, 'SE')
                self.assertEqual(home.price_unit, 'SEK/kWh')
                self.assertTrue(home.has_real_time_consumption)

                self.assertEqual(home.current_price_total, None)
                self.assertEqual(home.price_total, {})
                self.assertEqual(home.current_price_info, {})

                home.sync_update_current_price_info()
                self.assertTrue(home.current_price_total > 0)
                self.assertTrue(isinstance(home.current_price_info.get('energy'), (float, int)))
                self.assertTrue(isinstance(home.current_price_info.get('startsAt'), str))
                self.assertTrue(isinstance(home.current_price_info.get('tax'), (float, int)))
                self.assertTrue(isinstance(home.current_price_info.get('total'), (float, int)))

                home.sync_update_price_info()
                for key in home.price_total.keys():
                    self.assertTrue(isinstance(key, str))
                    self.assertTrue(isinstance(home.price_total[key], (float, int)))
            else:
                k += 1
                self.assertEqual(home.home_id, '68e6938b-91a6-4199-a0d4-f24c22be87bb')
                self.assertEqual(home.address1, 'Winterfell')
                self.assertEqual(home.country, 'NO')
                self.assertEqual(home.price_unit, ' ')
                self.assertTrue(home.has_real_time_consumption)

        self.assertEqual(k, 1)

    def test_update_info(self):
        self.assertEqual(len(self.tibber.get_homes()), 1)
        self.tibber.sync_update_info()
        self.assertEqual(len(self.tibber.get_homes()), 1)


class TestTibberWebsession(unittest.TestCase):
    """
    Tests Tibber
    """

    def setUp(self):  # pylint: disable=invalid-name
        """ things to be run when tests are started. """
        async def _create_session():
            return aiohttp.ClientSession()
        loop = asyncio.get_event_loop()
        self.websession = loop.run_until_complete(_create_session())
        self.tibber = tibber.Tibber(websession=self.websession)
        self.tibber.sync_update_info()

    def tearDown(self):  # pylint: disable=invalid-name
        """ Stop stuff we started. """
        self.tibber.sync_close_connection()

    def test_tibber(self):
        self.assertEqual(self.tibber.name, 'Arya Stark')
        self.assertEqual(len(self.tibber.get_homes()), 1)
        self.assertEqual(len(self.tibber.get_homes(only_active=False)), 1)

        home = self.tibber.get_homes()[0]
        self.tibber.sync_close_connection()
        self.assertRaises(RuntimeError, self.tibber.sync_update_info)

        self.assertRaises(RuntimeError, home.sync_update_info)
        self.assertEqual(home.address1, '')
        self.assertFalse(home.has_real_time_consumption)
        self.assertEqual(home.country, '')
        self.assertEqual(home.price_unit, ' ')


class TestTibberInvalidToken(unittest.TestCase):
    """
    Tests Tibber
    """

    def setUp(self):     # pylint: disable=invalid-name
        """ things to be run when tests are started. """
        self.tibber = tibber.Tibber(access_token='INVALID_TOKEN')        
        self.assertRaises(tibber.InvalidLogin, self.tibber.sync_update_info)

    def tearDown(self):  # pylint: disable=invalid-name
        """ Stop stuff we started. """
        self.tibber.sync_close_connection()

    def test_tibber(self):
        self.assertEqual(self.tibber.name, None)
        self.assertEqual(len(self.tibber.get_homes()), 0)

        async def run():
            self.assertFalse(await self.tibber.send_notification("Test tittle", "message"))
        loop = asyncio.get_event_loop()
        task = loop.create_task(run())
        loop.run_until_complete(task)


class TestTibberPrivateToken(unittest.TestCase):
    """
    Tests Tibber
    """

    def setUp(self):     # pylint: disable=invalid-name
        """ things to be run when tests are started. """
        self.tibber = tibber.Tibber(access_token='d11a43897efa4cf478afd659d6c8b7117da9e33b38232fd454b0e9f28af98012')
        self.tibber.sync_update_info()

    def tearDown(self):  # pylint: disable=invalid-name
        """ Stop stuff we started. """
        self.tibber.sync_close_connection()

    def test_tibber(self):
        self.assertEqual(self.tibber.name, 'Daniel Høyer')
        self.assertEqual(len(self.tibber.get_homes()), 0)
        self.assertEqual(len(self.tibber.get_homes(only_active=False)), 0)

    def test_invalid_home(self):
        home = self.tibber.get_home("INVALID_KEY")
        self.assertEqual(home, None)


if __name__ == '__main__':
    unittest.main()
