# -*- coding: utf-8 -*-
"""
Tests for pyTibber
"""
import unittest

import asyncio
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
        home = self.tibber.get_homes()[0]
        home.sync_update_info()
        self.assertEqual(home.home_id, 'c70dcbe5-4485-4821-933d-a8a86452737b')
        self.assertEqual(home.address1, 'Kungsgatan 8')
        self.assertEqual(home.country, 'SE')
        self.assertEqual(home.price_unit, 'SEK/kWh')

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


class TestTibberWebsession(unittest.TestCase):
    """
    Tests Tibber
    """

    def setUp(self):     # pylint: disable=invalid-name
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

        home = self.tibber.get_homes()[0]
        self.tibber.sync_close_connection()
        self.assertRaises(RuntimeError, self.tibber.sync_update_info)

        self.assertRaises(RuntimeError, home.sync_update_info)
        self.assertEqual(home.home_id, 'c70dcbe5-4485-4821-933d-a8a86452737b')
        self.assertEqual(home.address1, '')
        self.assertEqual(home.country, '')
        self.assertEqual(home.price_unit, ' ')


class TestTibberInvalidToken(unittest.TestCase):
    """
    Tests Tibber
    """

    def setUp(self):     # pylint: disable=invalid-name
        """ things to be run when tests are started. """
        self.tibber = tibber.Tibber(access_token='INVALID_TOKEN')
        self.tibber.sync_update_info()

    def tearDown(self):  # pylint: disable=invalid-name
        """ Stop stuff we started. """
        self.tibber.sync_close_connection()

    def test_tibber(self):
        self.assertEqual(self.tibber.name, None)
        self.assertEqual(len(self.tibber.get_homes()), 0)


if __name__ == '__main__':
    unittest.main()
