import unittest

from rest_framework import serializers

from waldur_core.core import utils as core_utils

from waldur_openstack.openstack_tenant.fields import StringTimestampField


class StringTimestampFieldTest(unittest.TestCase):
    def setUp(self):
        self.field = StringTimestampField(formats=('%Y-%m-%dT%H:%M:%S',))
        self.datetime = core_utils.timeshift()
        self.datetime_str = self.datetime.strftime('%Y-%m-%dT%H:%M:%S')
        self.timestamp = core_utils.datetime_to_timestamp(self.datetime)

    def test_datetime_string_serialized_as_timestamp(self):
        actual = self.field.to_representation(self.datetime_str)
        self.assertEqual(self.timestamp, actual)

    def test_timestamp_parsed_as_datetime_string(self):
        actual = self.field.to_internal_value(self.timestamp)
        self.assertEqual(self.datetime_str, actual)

    def test_incorrect_timestamp(self):
        with self.assertRaises(serializers.ValidationError):
            self.field.to_internal_value("NOT_A_UNIX_TIMESTAMP")

    def test_incorrect_datetime_string(self):
        datetime_str = self.datetime.strftime('%Y-%m-%d')
        with self.assertRaises(serializers.ValidationError):
            self.field.to_representation(datetime_str)

    def test_field_initialization_without_formats(self):
        with self.assertRaises(AssertionError):
            StringTimestampField()
