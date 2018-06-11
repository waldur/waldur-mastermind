from __future__ import unicode_literals

import six

from django.core.exceptions import ValidationError
from django.utils.translation import ugettext_lazy as _

from . import utils

ATTRIBUTE_TYPES = []


class AttributeTypeMeta(type):
    def __new__(mcs, class_name, bases, attrs):
        if class_name.endswith('Attribute'):
            name = class_name.replace('Attribute', '')
            name = utils.camel_to_snake(name)
            ATTRIBUTE_TYPES.append((name, name))

        return super(AttributeTypeMeta, mcs).__new__(mcs, class_name, bases, attrs)


@six.add_metaclass(AttributeTypeMeta)
class AttributeType(object):
    @staticmethod
    def available_values_validate(values):
        if values:
            raise ValidationError(_("Available values must be empty for this attribute type."))


class BooleanAttribute(AttributeType):
    pass


class StringAttribute(AttributeType):
    pass


class IntegerAttribute(AttributeType):
    pass


class ChoiceAttribute(AttributeType):
    @staticmethod
    def available_values_validate(values):
        if not values:
            raise ValidationError(_("Available values not must be empty for this attribute type."))

        if not isinstance(values, list):
            raise ValidationError(_("Available values must be a list for this attribute type."))


class ListAttribute(AttributeType):
    @staticmethod
    def available_values_validate(values):
        if not values:
            raise ValidationError(_("Available values not must be empty for this attribute type."))

        if not isinstance(values, list):
            raise ValidationError(_("Available values must be a list for this attribute type."))
