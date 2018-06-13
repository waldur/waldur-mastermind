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

    @staticmethod
    def validate(values, available_values):
        raise NotImplementedError


class BooleanAttribute(AttributeType):
    @staticmethod
    def validate(values, available_values=None):
        if not isinstance(values, bool):
            raise ValidationError(_("Value must be a boolean type  for this attribute type."))


class StringAttribute(AttributeType):
    @staticmethod
    def validate(values, available_values=None):
        if not isinstance(values, six.text_type):
            raise ValidationError(_("Value must be a boolean type  for this attribute type."))


class IntegerAttribute(AttributeType):
    @staticmethod
    def validate(values, available_values=None):
        if not isinstance(values, int):
            raise ValidationError(_("Value must be an integer type  for this attribute type."))


class ChoiceAttribute(AttributeType):
    @staticmethod
    def available_values_validate(values):
        if not values:
            raise ValidationError(_("Available values not must be empty for this attribute type."))

        if not isinstance(values, list):
            raise ValidationError(_("Available values must be a list for this attribute type."))

    @staticmethod
    def validate(values, available_values):
        if not isinstance(values, six.text_type):
            raise ValidationError(_("Value must be a string for this attribute."))

        if not(values in available_values):
            raise ValidationError(_("This value is not available for this attribute.") %
                                  set(values) - set(available_values))


class ListAttribute(AttributeType):
    @staticmethod
    def available_values_validate(values):
        if not values:
            raise ValidationError(_("Available values not must be empty for this attribute type."))

        if not isinstance(values, list):
            raise ValidationError(_("Available values must be a list for this attribute type."))

    @staticmethod
    def validate(values, available_values):
        if not isinstance(values, list):
            raise ValidationError(_("Value must be a list for this attribute."))

        if not(set(values) <= set(available_values)):
            raise ValidationError(_("These values are not available for this attribute."))
