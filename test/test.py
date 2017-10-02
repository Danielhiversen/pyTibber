# -*- coding: utf-8 -*-
"""
Tests for pyTibber
"""

import unittest

import Tibber


class TestTibber(unittest.TestCase):
    """
    Tests Tibber
    """

    def setUp(self):     # pylint: disable=invalid-name
        """ things to be run when tests are started. """
        self.tibber = Tibber.Tibber()
        self.tibber.sync_update_info()

    def tearDown(self):  # pylint: disable=invalid-name
        """ Stop stuff we started. """
        self.tibber.websession.close()

    def test_tibber(self):
        self.assertEqual(self.tibber.name, 'Arya Stark')
        self.assertEqual(len(self.tibber.get_homes()), 1)

    def test_home(self):
        home = self.tibber.get_homes()[0]
        home.sync_update_info()

        self.assertEqual(home.current_price_total, None)
        self.assertEqual(home.price_total, {})
        self.assertEqual(home.current_price_info, {})

        home.sync_update_current_price_info()
        self.assertTrue(home.current_price_total > 0)
        self.assertTrue(isinstance(home.current_price_info.get('energy'), float))
        self.assertTrue(isinstance(home.current_price_info.get('startsAt'), str))
        self.assertTrue(isinstance(home.current_price_info.get('tax'), float))
        self.assertTrue(isinstance(home.current_price_info.get('total'), float))

        home.sync_update_price_info()
        for key in home.price_total.keys():
            self.assertTrue(isinstance(key, str))
            self.assertTrue(isinstance(home.price_total[key], float))

        self.assertEqual(home.home_id, 'c70dcbe5-4485-4821-933d-a8a86452737b')
        self.assertEqual(home.address1, 'Förmansvägen 21 Lgh 1502')
        self.assertEqual(home.country, 'SE')
        self.assertEqual(home.price_unit, 'SEK/kWh')


if __name__ == '__main__':
    unittest.main()
