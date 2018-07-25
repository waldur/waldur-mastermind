import unittest

from .. import utils


class TimeParserTest(unittest.TestCase):
    def test_time_without_suffix_is_treated_as_seconds(self):
        self.assertEqual(utils.parse_time('600'), 600)

    def test_time_with_seconds_suffix_is_parsed_correctly(self):
        self.assertEqual(utils.parse_time('600s'), 600)

    def test_time_with_minutes_suffix_is_multiplied(self):
        self.assertEqual(utils.parse_time('10m'), 600)

    def test_time_with_invalid_unit_raises_error(self):
        self.assertRaises(ValueError, utils.parse_time, '10y')

    def test_invalid_input_value_raises_error(self):
        self.assertRaises(ValueError, utils.parse_time, 'y10')
