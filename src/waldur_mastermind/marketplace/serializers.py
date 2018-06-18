from __future__ import unicode_literals

import json

from rest_framework import serializers
from rest_framework import exceptions as rest_exceptions

from waldur_core.core import serializers as core_serializers
from django.core.exceptions import ValidationError
from waldur_core.structure import permissions as structure_permissions
from waldur_core.core import fields as core_fields

from . import models, utils, attribute_types


class ServiceProviderSerializer(core_serializers.AugmentedSerializerMixin,
                                serializers.HyperlinkedModelSerializer):

    class Meta(object):
        model = models.ServiceProvider
        fields = ('url', 'uuid', 'created', 'customer', 'customer_name', 'enable_notifications')
        read_only_fields = ('url', 'uuid', 'created')
        related_paths = {
            'customer': ('uuid', 'name', 'native_name', 'abbreviation')
        }
        protected_fields = ('customer',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'marketplace-service-provider-detail'},
            'customer': {'lookup_field': 'uuid'},
        }

    def validate(self, attrs):
        if self.instance:
            structure_permissions.is_owner(self.context['request'], None, self.instance.customer)
            return attrs

        structure_permissions.is_owner(self.context['request'], None, attrs['customer'])
        return attrs


class CategorySerializer(core_serializers.AugmentedSerializerMixin,
                         serializers.HyperlinkedModelSerializer):
    offerings_count = serializers.SerializerMethodField()

    def get_offerings_count(self, category):
        return category.offerings.count()

    class Meta(object):
        model = models.Category
        fields = ('url', 'uuid', 'created', 'title', 'description', 'icon', 'offerings_count')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'marketplace-category-detail'},
        }


class AttributesSerializer(core_fields.JsonField):
    def to_representation(self, attributes):
        return utils.hstore_to_dict(attributes)


class OfferingSerializer(core_serializers.AugmentedSerializerMixin,
                         serializers.HyperlinkedModelSerializer):
    attributes = AttributesSerializer(required=False)

    class Meta(object):
        model = models.Offering
        fields = ('url', 'uuid', 'created', 'name', 'description', 'full_description', 'provider', 'category',
                  'rating', 'attributes', 'geolocations', 'is_active')
        read_only_fields = ('url', 'uuid', 'created')
        protected_fields = ('provider',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'marketplace-offering-detail'},
            'provider': {'lookup_field': 'uuid', 'view_name': 'marketplace-service-provider-detail'},
            'category': {'lookup_field': 'uuid', 'view_name': 'marketplace-category-detail'},
        }

    def validate(self, attrs):
        if self.instance:
            structure_permissions.is_owner(self.context['request'], None, self.instance.provider.customer)
        else:
            structure_permissions.is_owner(self.context['request'], None, attrs['provider'].customer)

        offering_attributes = attrs.get('attributes')

        if offering_attributes:
            category = attrs.get('category', getattr(self.instance, 'category', None))
            self._validate_attributes(offering_attributes, category)
            attrs['attributes'] = utils.dict_to_hstore(offering_attributes)

        return attrs

    def _validate_attributes(self, offering_attributes, category):
        offering_attribute_keys = offering_attributes.keys()
        category_attributes = list(models.Attribute.objects.filter(section__category=category,
                                                                   key__in=offering_attribute_keys))
        for key, value in offering_attributes.items():
            match_attributes = filter(lambda a: a.key == key, category_attributes)
            attribute = match_attributes[0] if match_attributes else None

            if attribute:
                klass = attribute_types.get_attribute_type(attribute.type)
                if klass:
                    try:
                        klass.validate(value, attribute.available_values)
                    except ValidationError as e:
                        err = rest_exceptions.ValidationError({'attributes': e.message})
                        raise err
