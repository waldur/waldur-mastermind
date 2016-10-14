from django.db import transaction
from django.template.defaultfilters import slugify
from rest_framework import serializers

from nodeconductor.core import utils as core_utils
from nodeconductor_openstack import apps as openstack_apps, models as openstack_models

from . import models


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


class OpenStackPackageSerializer(serializers.HyperlinkedModelSerializer):
    name = serializers.CharField(source='tenant.name', help_text='Tenant name.')
    description = serializers.CharField(
        initial='', required=False, allow_blank=True, source='tenant.description', help_text='Tenant description.')
    service_project_link = serializers.HyperlinkedRelatedField(
        source='tenant.service_project_link',
        view_name='openstack-spl-detail', write_only=True,
        queryset=openstack_models.OpenStackServiceProjectLink.objects.all())
    user_username = serializers.CharField(
        source='tenant.user_username', required=False, allow_null=True,
        help_text='Tenant user username. By default is generated as <tenant name> + "-user".')
    availability_zone = serializers.CharField(
        source='tenant.availability_zone', initial='', required=False, allow_blank=True,
        help_text='Tenant availability zone.')

    class Meta(object):
        model = models.OpenStackPackage
        fields = ('url', 'uuid', 'name', 'description', 'template', 'service_project_link', 'user_username',
                  'availability_zone', 'tenant', 'service_settings',)
        view_name = 'openstack-package-detail'
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'template': {'lookup_field': 'uuid', 'view_name': 'package-template-detail'},
            'tenant': {'lookup_field': 'uuid', 'view_name': 'openstack-tenant-detail', 'read_only': True},
            'service_settings': {'lookup_field': 'uuid', 'read_only': True},
        }

    def validate_template(self, template):
        if template.service_settings.type != openstack_apps.OpenStackConfig.service_name:
            raise serializers.ValidationError('Package template should be related to OpenStack server.')
        return template

    def validate(self, attrs):
        spl = attrs['tenant']['service_project_link']
        template = attrs['template']
        if spl.service.settings != template.service_settings:
            raise serializers.ValidationError(
                'Template and service project link should be connected to the same service settings.')
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        """ Create tenant and service settings from it """
        template = validated_data['template']
        tenant_data = validated_data['tenant']
        if not tenant_data['availability_zone']:
            tenant_data['availability_zone'] = template.service_settings.get_option('availability_zone') or ''
        if not tenant_data['user_username']:
            tenant_data['user_username'] = slugify(tenant_data['name'])[:30] + '-user'
        validated_data['tenant'] = tenant = openstack_models.Tenant.objects.create(
            user_password=core_utils.pwgen(), extra_configuration={'package': template.name}, **tenant_data)
        self._set_tenant_quotas(tenant, template)
        service = tenant.create_service()
        validated_data['service_settings'] = service.settings
        return super(OpenStackPackageSerializer, self).create(validated_data)

    def _set_tenant_quotas(self, tenant, template):
        components = {c.type: c.amount for c in template.components.all()}
        quotas = {
            openstack_models.Tenant.Quotas.ram: components[models.PackageComponent.Types.RAM],
            openstack_models.Tenant.Quotas.vcpu: components[models.PackageComponent.Types.CORES],
            openstack_models.Tenant.Quotas.storage: components[models.PackageComponent.Types.STORAGE],
        }
        for name, limit in quotas.items():
            tenant.set_quota_limit(name, limit)
