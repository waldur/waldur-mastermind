from rest_framework import serializers

from . import models


class OpenStackItemSerializer(serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.OpenStackItem
        fields = ('package_details', 'package', 'name', 'price', 'start', 'end')
        extra_kwargs = {
            'package': {'lookup_field': 'uuid', 'view_name': 'openstack-package-detail'},
        }


class InvoiceSerializer(serializers.HyperlinkedModelSerializer):
    total = serializers.DecimalField(max_digits=15, decimal_places=7)
    openstack_items = OpenStackItemSerializer(many=True)

    class Meta(object):
        model = models.Invoice
        fields = (
            'url', 'uuid', 'customer', 'total', 'openstack_items', 'state', 'year', 'month'
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'customer': {'lookup_field': 'uuid'},
        }
