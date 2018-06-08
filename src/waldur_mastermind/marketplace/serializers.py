from __future__ import unicode_literals

from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import permissions as structure_permissions

from . import models


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
            'url': {'lookup_field': 'uuid', 'view_name': 'service-provider-detail'},
            'customer': {'lookup_field': 'uuid'},
        }

    def validate(self, attrs):
        if self.instance:
            structure_permissions.is_owner(self.context['request'], None, self.instance.customer)
            return attrs

        structure_permissions.is_owner(self.context['request'], None, attrs['customer'])
        return attrs
