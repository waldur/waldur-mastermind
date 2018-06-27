from __future__ import unicode_literals

from django.conf import settings
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions as rest_exceptions
from rest_framework import serializers

from waldur_core.core import fields as core_fields
from waldur_core.core import serializers as core_serializers
from waldur_core.structure import permissions as structure_permissions

from . import models, attribute_types


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
        if not self.instance:
            structure_permissions.is_owner(self.context['request'], None, attrs['customer'])
        return attrs


class CategorySerializer(core_serializers.AugmentedSerializerMixin,
                         serializers.HyperlinkedModelSerializer):
    offering_count = serializers.SerializerMethodField()

    def get_offering_count(self, category):
        try:
            return category.quotas.get(name='offering_count').usage
        except ObjectDoesNotExist:
            return 0

    class Meta(object):
        model = models.Category
        fields = ('url', 'uuid', 'created', 'title', 'description', 'icon', 'offering_count')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'marketplace-category-detail'},
        }


class OfferingSerializer(core_serializers.AugmentedSerializerMixin,
                         serializers.HyperlinkedModelSerializer):
    preferred_language = serializers.ChoiceField(choices=settings.LANGUAGES, allow_blank=True, required=False)
    attributes = core_fields.JsonField(required=False)

    class Meta(object):
        model = models.Offering
        fields = ('url', 'uuid', 'created', 'name', 'description', 'full_description', 'provider', 'category',
                  'rating', 'attributes', 'geolocations', 'is_active', 'native_name', 'native_description',
                  'preferred_language')
        read_only_fields = ('url', 'uuid', 'created')
        protected_fields = ('provider',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'marketplace-offering-detail'},
            'provider': {'lookup_field': 'uuid', 'view_name': 'marketplace-service-provider-detail'},
            'category': {'lookup_field': 'uuid', 'view_name': 'marketplace-category-detail'},
        }

    def validate(self, attrs):
        if not self.instance:
            structure_permissions.is_owner(self.context['request'], None, attrs['provider'].customer)

        offering_attributes = attrs.get('attributes')

        if offering_attributes:
            category = attrs.get('category', getattr(self.instance, 'category', None))
            self._validate_attributes(offering_attributes, category)

        self._validate_language(attrs)
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

    def _validate_language(self, attrs):
        language = attrs.get('preferred_language')
        native_name = attrs.get('native_name')
        native_description = attrs.get('native_description')

        if not language and (native_name or native_description):
            raise rest_exceptions.ValidationError(
                {'preferred_language': _('This field is required if native_name or native_description is specified.')}
            )


class ScreenshotSerializer(core_serializers.AugmentedSerializerMixin,
                           serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.Screenshots
        fields = ('url', 'uuid', 'created', 'name', 'description', 'image', 'thumbnail', 'offering')
        read_only_fields = ('url', 'uuid', 'created')
        protected_fields = ('offering', 'image')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'offering': {'lookup_field': 'uuid', 'view_name': 'marketplace-offering-detail'},
        }

    def validate(self, attrs):
        if not self.instance:
            structure_permissions.is_owner(self.context['request'], None, attrs['offering'].provider.customer)
        return attrs
