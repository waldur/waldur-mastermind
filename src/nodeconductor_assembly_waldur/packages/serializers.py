from rest_framework import serializers

from nodeconductor_assembly_waldur.packages import models


class PackageComponentSerializer(serializers.ModelSerializer):
    price = serializers.DecimalField(max_digits=6, decimal_places=2, coerce_to_string=False)

    class Meta(object):
        model = models.PackageComponent
        fields = ('type', 'amount', 'price')


class PackageTemplateSerializer(serializers.HyperlinkedModelSerializer):
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
