from django.db import transaction
from django.template.defaultfilters import slugify
from rest_framework import serializers

from nodeconductor.core import utils as core_utils
from nodeconductor.structure import serializers as structure_serializers, models as structure_models
from nodeconductor_openstack.openstack import apps as openstack_apps, models as openstack_models
from nodeconductor_openstack.openstack_tenant import models as openstack_tenant_models, apps as openstack_tenant_apps

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
            'url', 'uuid', 'name', 'description', 'service_settings', 'price', 'icon_url', 'components',
        )
        view_name = 'package-template-detail'
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'service_settings': {'lookup_field': 'uuid'},
        }


class OpenStackPackageSerializer(
        structure_serializers.PermissionFieldFilteringMixin, serializers.HyperlinkedModelSerializer):
    name = serializers.CharField(source='tenant.name', help_text='Tenant name.')
    description = serializers.CharField(
        required=False, allow_blank=True, source='tenant.description', help_text='Tenant description.')
    service_project_link = serializers.HyperlinkedRelatedField(
        source='tenant.service_project_link',
        view_name='openstack-spl-detail', write_only=True,
        queryset=openstack_models.OpenStackServiceProjectLink.objects.all())
    user_username = serializers.CharField(
        source='tenant.user_username', required=False, allow_null=True,
        help_text='Tenant user username. By default is generated as <tenant name> + "-user".')
    user_password = serializers.CharField(
        source='tenant.user_password', required=False, allow_null=True,
        help_text='Tenant user password. Leave blank if you want admin password to be auto-generated.')
    availability_zone = serializers.CharField(
        source='tenant.availability_zone', required=False, allow_blank=True,
        help_text='Tenant availability zone.')

    class Meta(object):
        model = models.OpenStackPackage
        fields = ('url', 'uuid', 'name', 'description', 'template', 'service_project_link',
                  'user_username', 'user_password', 'availability_zone', 'tenant', 'service_settings',)
        view_name = 'openstack-package-detail'
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'template': {'lookup_field': 'uuid', 'view_name': 'package-template-detail'},
            'tenant': {'lookup_field': 'uuid', 'view_name': 'openstack-tenant-detail', 'read_only': True},
            'service_settings': {'lookup_field': 'uuid', 'read_only': True},
        }

    def get_filtered_field_names(self):
        return ('service_settings', 'service_project_link')

    def validate_template(self, template):
        if template.service_settings.type != openstack_apps.OpenStackConfig.service_name:
            raise serializers.ValidationError('Package template should be related to OpenStack service settings.')
        return template

    def validate_service_project_link(self, spl):
        user = self.context['request'].user
        if (not user.is_staff and not spl.project.has_user(user, structure_models.ProjectRole.MANAGER) and
                not spl.project.customer.has_user(user, structure_models.CustomerRole.OWNER)):
            raise serializers.ValidationError('Only staff, owner or manager can order package.')
        return spl

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
        if not tenant_data.get('availability_zone'):
            tenant_data['availability_zone'] = template.service_settings.get_option('availability_zone') or ''
        if not tenant_data.get('user_username'):
            tenant_data['user_username'] = slugify(tenant_data['name'])[:30] + '-user'
        if not tenant_data.get('user_password'):
            tenant_data['user_password'] = core_utils.pwgen()
        extra_configuration = {'package_name': template.name, 'package_uuid': template.uuid.hex}
        validated_data['tenant'] = tenant = openstack_models.Tenant.objects.create(
            extra_configuration=extra_configuration, **tenant_data)
        self._set_tenant_quotas(tenant, template)
        service_settings = self._create_service_settings(tenant)
        validated_data['service_settings'] = service_settings
        return super(OpenStackPackageSerializer, self).create(validated_data)

    def _create_service_settings(self, tenant):
        """ Create service settings from tenant and connect them to tenant project. """
        admin_settings = tenant.service_project_link.service.settings
        customer = tenant.service_project_link.project.customer
        service_settings = structure_models.ServiceSettings.objects.create(
            name=tenant.name,
            scope=tenant,
            customer=customer,
            type=openstack_tenant_apps.OpenStackTenantConfig.service_name,
            backend_url=admin_settings.backend_url,
            username=tenant.user_username,
            password=tenant.user_password,
            options={
                'availability_zone': tenant.availability_zone,
                'tenant_id': tenant.backend_id,
            },
        )
        service = openstack_tenant_models.OpenStackTenantService.objects.create(
            name=tenant.name,
            settings=service_settings,
            customer=customer,
        )
        openstack_tenant_models.OpenStackTenantServiceProjectLink.objects.create(
            service=service,
            project=tenant.service_project_link.project,
        )
        return service_settings

    def _set_tenant_quotas(self, tenant, template):
        components = {c.type: c.amount for c in template.components.all()}
        for quota_name, component_type in models.OpenStackPackage.get_quota_to_component_mapping().items():
            tenant.set_quota_limit(quota_name, components[component_type])
