from __future__ import unicode_literals

import copy
import json
import uuid

from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils.encoding import smart_text
from django.utils.translation import ugettext_lazy as _
import pycountry
from rest_framework import serializers
import six

from waldur_core.core import utils, validators as core_validators
from waldur_core.core.validators import validate_cron_schedule


class CronScheduleField(models.CharField):
    description = "A cron schedule in textual form"

    def __init__(self, *args, **kwargs):
        kwargs['validators'] = [validate_cron_schedule] + kwargs.get('validators', [])
        kwargs['max_length'] = kwargs.get('max_length', 15)
        super(CronScheduleField, self).__init__(*args, **kwargs)


class MappedChoiceField(serializers.ChoiceField):
    """
    A choice field that maps enum values from representation to model ones and back.

    :Example:

    >>> # models.py
    >>> class IceCream(models.Model):
    >>>     class Meta:
    >>>         app_label = 'myapp'
    >>>
    >>>     CHOCOLATE = 0
    >>>     VANILLA = 1
    >>>
    >>>     FLAVOR_CHOICES = (
    >>>         (CHOCOLATE, _('Chocolate')),
    >>>         (VANILLA, _('Vanilla')),
    >>>     )
    >>>
    >>>     flavor = models.SmallIntegerField(choices=FLAVOR_CHOICES)
    >>>
    >>> # serializers.py
    >>> class IceCreamSerializer(serializers.ModelSerializer):
    >>>     class Meta:
    >>>         model = IceCream
    >>>
    >>>     flavor = MappedChoiceField(
    >>>         choices={
    >>>             'chocolate': _('Chocolate'),
    >>>             'vanilla': _('Vanilla'),
    >>>         },
    >>>         choice_mappings={
    >>>             'chocolate': IceCream.CHOCOLATE,
    >>>             'vanilla': IceCream.VANILLA,
    >>>         },
    >>>     )
    >>>
    >>> model1 = IceCream(flavor=IceCream.CHOCOLATE)
    >>> serializer1 = IceCreamSerializer(instance=model1)
    >>> serializer1.data
    {'flavor': 'chocolate', u'id': None}
    >>>
    >>> data2 = {'flavor': 'vanilla'}
    >>> serializer2 = IceCreamSerializer(data=data2)
    >>> serializer2.is_valid()
    True
    >>> serializer2.validated_data["flavor"] == IceCream.VANILLA
    True
    """

    def __init__(self, choice_mappings, **kwargs):
        super(MappedChoiceField, self).__init__(**kwargs)

        assert set(self.choices.keys()) == set(choice_mappings.keys()), 'Choices do not match mappings'
        assert len(set(choice_mappings.values())) == len(choice_mappings), 'Mappings are not unique'

        self.mapped_to_model = choice_mappings
        self.model_to_mapped = {v: k for k, v in six.iteritems(choice_mappings)}

    def to_internal_value(self, data):
        if data == '' and self.allow_blank:
            return ''

        data = super(MappedChoiceField, self).to_internal_value(data)

        try:
            return self.mapped_to_model[six.text_type(data)]
        except KeyError:
            self.fail('invalid_choice', input=data)

    def to_representation(self, value):
        if value in ('', None):
            return value

        value = self.model_to_mapped[value]

        return super(MappedChoiceField, self).to_representation(value)


class NaturalChoiceField(MappedChoiceField):
    def __init__(self, choices=None, **kwargs):
        super(NaturalChoiceField, self).__init__(
            choices=[(v, v) for k, v in choices],
            choice_mappings={v: k for k, v in choices},
            **kwargs)


class TimestampField(serializers.Field):
    """
    Unix timestamp field mapped to datetime object.
    """

    def to_representation(self, value):
        return utils.datetime_to_timestamp(value)

    def to_internal_value(self, value):
        try:
            return utils.timestamp_to_datetime(value)
        except ValueError:
            raise serializers.ValidationError(_('Value "%s" should be valid UNIX timestamp.') % value)


class CountryField(models.CharField):

    COUNTRIES = [(country.alpha2, country.name) for country in pycountry.countries]

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('max_length', 2)
        kwargs.setdefault('choices', CountryField.COUNTRIES)

        super(CountryField, self).__init__(*args, **kwargs)


class StringUUID(uuid.UUID):
    """
    Default UUID class __str__ method returns hyphenated string.
    This class returns non-hyphenated string.
    """

    def __unicode__(self):
        return six.text_type(str(self))

    def __str__(self):
        return self.hex

    def __len__(self):
        return len(self.__unicode__())


class UUIDField(models.UUIDField):
    """
    This class implements backward-compatible non-hyphenated rendering of UUID values.
    Default field parameters are not exposed in migrations.
    """

    def __init__(self, **kwargs):
        kwargs['default'] = lambda: StringUUID(uuid.uuid4().hex)
        kwargs['editable'] = False
        kwargs['unique'] = True
        super(UUIDField, self).__init__(**kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super(UUIDField, self).deconstruct()
        del kwargs['default']
        del kwargs['editable']
        del kwargs['unique']
        return name, path, args, kwargs

    def _parse_uuid(self, value):
        if not value:
            return None
        return StringUUID(smart_text(value))

    def from_db_value(self, value, expression, connection, context):
        return self._parse_uuid(value)

    def to_python(self, value):
        return self._parse_uuid(value)


class BackendURLField(models.URLField):
    default_validators = [core_validators.BackendURLValidator()]


class JSONField(models.TextField):
    def __init__(self, *args, **kwargs):
        self.dump_kwargs = kwargs.pop('dump_kwargs', {
            'cls': DjangoJSONEncoder,
            'separators': (',', ':')
        })
        self.load_kwargs = kwargs.pop('load_kwargs', {})

        super(JSONField, self).__init__(*args, **kwargs)

    def from_db_value(self, value, expression, connection, context):
        return self.to_python(value)

    def to_python(self, value):
        if isinstance(value, six.string_types) and value:
            try:
                return json.loads(value, **self.load_kwargs)
            except ValueError:
                raise ValidationError(_('Enter valid JSON'))
        return value

    def get_prep_value(self, value):
        """Convert JSON object to a string"""
        if self.null and value is None:
            return None
        return json.dumps(value, **self.dump_kwargs)

    def get_default(self):
        """
        Returns the default value for this field.
        The default implementation on models.Field calls force_unicode
        on the default, which means you can't set arbitrary Python
        objects as the default. To fix this, we just return the value
        without calling force_unicode on it. Note that if you set a
        callable as a default, the field will still call it. It will
        *not* try to pickle and encode it.
        """
        if self.has_default():
            if callable(self.default):
                return self.default()
            return copy.deepcopy(self.default)
        # If the field doesn't have a default, then we punt to models.Field.
        return super(JSONField, self).get_default()
