from __future__ import unicode_literals

import unittest

from waldur_core.core import utils


class TestFormatTimeAndValueToSegmentList(unittest.TestCase):

    def test_function_splits_time_to_segments_right(self):
        start_timestamp = 100
        end_timestamp = 200
        segments_count = 5
        # when
        segment_list = utils.format_time_and_value_to_segment_list(
            [], segments_count, start_timestamp, end_timestamp)
        # then
        self.assertEqual(len(segment_list), segments_count)
        step = (end_timestamp - start_timestamp) / segments_count
        for index, segment in enumerate(segment_list):
            self.assertEqual(segment['from'], start_timestamp + step * index)
            self.assertEqual(segment['to'], start_timestamp + step * (index + 1))

    def test_function_sums_values_in_segments_right(self):
        start_timestamp = 20
        end_timestamp = 60
        segments_count = 2
        first_segment_time_value_list = [(22, 1), (23, 2)]
        second_segment_time_value_list = [(52, 2), (59, 2), (43, 3)]
        time_and_value_list = first_segment_time_value_list + second_segment_time_value_list
        # when
        segment_list = utils.format_time_and_value_to_segment_list(
            time_and_value_list, segments_count, start_timestamp, end_timestamp)
        # then
        first_segment, second_segment = segment_list
        expected_first_segment_value = sum([value for _, value in first_segment_time_value_list])
        expected_second_segment_value = sum([value for _, value in second_segment_time_value_list])
        self.assertEqual(first_segment['value'], expected_first_segment_value)
        self.assertEqual(second_segment['value'], expected_second_segment_value)
