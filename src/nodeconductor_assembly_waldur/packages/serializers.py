from rest_framework import serializers

from nodeconductor_assembly_waldur.packages import models


class PackageComponentSerializer(serializers.ModelSerializer):

    class Meta(object):
        model = models.PackageComponent
        fields = ('type', 'amount', 'price')


class PackageTemplateSerializer(serializers.HyperlinkedModelSerializer):
    price = serializers.DecimalField(max_digits=13, decimal_places=7)
    components = PackageComponentSerializer(many=True)

    class Meta(object):
        model = models.PackageTemplate
        fields = (
            'url', 'uuid', 'name', 'description', 'type', 'price', 'icon_url', 'components'
        )
        view_name = 'package-template-detail'
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }
