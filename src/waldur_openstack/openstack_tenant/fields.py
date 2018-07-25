from datetime import datetime

from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers

from waldur_core.core import utils as core_utils


class StringTimestampField(serializers.CharField):
    """
    This field serializes datetime string representation to the timestamp (e.g. '2016-08-11T10:33:38' -> 1470911618).
    Note that there must be at least one format provided in formats parameter (e.g. formats=('%Y-%m-%dT%H:%M:%S',)).
    First format from formats list will be used during deserialization process.
    """

    def __init__(self, formats=(), **kwargs):
        assert formats, 'At least one datetime string format must be provided.'
        self.formats = formats
        super(StringTimestampField, self).__init__(**kwargs)

    def to_representation(self, value):
        for str_format in self.formats:
            try:
                date_time = datetime.strptime(value, str_format)
            except ValueError:
                pass
            else:
                return core_utils.datetime_to_timestamp(date_time)

        raise serializers.ValidationError(_('This field does not have datetime format that matches %s string.') % value)

    def to_internal_value(self, value):
        try:
            date_time = core_utils.timestamp_to_datetime(value)
            datetime_str = date_time.strftime(self.formats[0])
        except ValueError:
            raise serializers.ValidationError(_('Value "{}" should be valid UNIX timestamp.').format(value))
        return datetime_str
