from __future__ import unicode_literals

import json

from rest_framework import serializers
from rest_framework import exceptions as rest_exceptions

from waldur_core.core import serializers as core_serializers
from django.core.exceptions import ValidationError
from waldur_core.structure import permissions as structure_permissions

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


class AttributesSerializer(serializers.Field):
    def to_internal_value(self, data):
        if not data:
            return ''
        data = json.loads(data)
        return utils.dict_to_hstore(data)

    def to_representation(self, attributes):
        return utils.hstore_to_dict(attributes)


class OfferingSerializer(core_serializers.AugmentedSerializerMixin,
                         serializers.HyperlinkedModelSerializer):
    attributes = AttributesSerializer()

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

        if attrs.get('attributes'):
            offering_attributes = utils.hstore_to_dict(attrs['attributes'])
            offering_attribute_keys = offering_attributes.keys()
            attributes = list(models.Attribute.objects.filter(section__category=attrs['category'],
                                                              key__in=offering_attribute_keys))
            for key, value in offering_attributes.items():
                attribute = filter(lambda a: a.key == key, attributes)[0] if filter(lambda a: a.key == key, attributes) \
                    else None
                if attribute:
                    klass_name = utils.snake_to_camel(attribute.type) + 'Attribute'
                    klass = getattr(attribute_types, klass_name)
                    try:
                        klass.validate(value, attribute.available_values)
                    except ValidationError as e:
                        err = rest_exceptions.ValidationError({'attributes': e.message})
                        raise err

        return attrs
