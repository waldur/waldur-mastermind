from __future__ import unicode_literals

from django.conf import settings
from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import serializers as structure_serializers, models as structure_models
from waldur_openstack.openstack import (
    apps as openstack_apps, models as openstack_models, serializers as openstack_serializers)
from waldur_openstack.openstack_tenant import apps as openstack_tenant_apps

from . import models


class PackageComponentSerializer(serializers.ModelSerializer):
    class Meta(object):
        model = models.PackageComponent
        fields = ('type', 'amount', 'price')


class PackageTemplateSerializer(core_serializers.AugmentedSerializerMixin,
                                serializers.HyperlinkedModelSerializer):
    price = serializers.DecimalField(max_digits=22, decimal_places=10)
    monthly_price = serializers.DecimalField(max_digits=16, decimal_places=2)
    components = PackageComponentSerializer(many=True)
    category = serializers.ReadOnlyField(source='get_category_display')

    class Meta(object):
        model = models.PackageTemplate
        fields = (
            'url', 'uuid', 'name', 'description', 'service_settings',
            'price', 'monthly_price', 'icon_url', 'components', 'category', 'archived',
            'product_code', 'article_code',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'service_settings': {'lookup_field': 'uuid'},
        }


def _check_template_service_settings(serializer, template):
    """ Template service settings should be in state OK and has type OpenStack """
    if template.service_settings.type != openstack_apps.OpenStackConfig.service_name:
        raise serializers.ValidationError(_('Template should be related to OpenStack service settings.'))
    elif template.service_settings.state != structure_models.ServiceSettings.States.OK:
        raise serializers.ValidationError(_('Template\'s settings must be in OK state.'))
    return template


def _get_template_quotas(template):
    components = {c.type: c.amount for c in template.components.all()}
    mapping = models.OpenStackPackage.get_quota_to_component_mapping()
    return {
        quota_name: components[component_type]
        for quota_name, component_type in mapping.items()
    }


def _apply_quotas(target, quotas):
    for name, limit in quotas.items():
        target.set_quota_limit(name, limit)


def _set_tenant_quotas(tenant, template):
    quotas = _get_template_quotas(template)
    _apply_quotas(tenant, quotas)


def _set_related_service_settings_quotas(tenant, template):
    quotas = _get_template_quotas(template)
    for target in structure_models.ServiceSettings.objects.filter(scope=tenant):
        _apply_quotas(target, quotas)


def _set_tenant_extra_configuration(tenant, template):
    tenant.extra_configuration = {
        'package_name': template.name,
        'package_uuid': template.uuid.hex,
        'package_category': template.get_category_display(),
    }
    for component in template.components.all():
        tenant.extra_configuration[component.type] = component.amount
    tenant.save()


def _has_access_to_package(user, spl):
    """ Staff and owner always have access to package. Manager - only if correspondent flag is enabled """
    manager_can_create = settings.WALDUR_OPENSTACK['MANAGER_CAN_MANAGE_TENANTS']
    return (
        user.is_staff or
        spl.service.customer.has_user(user, structure_models.CustomerRole.OWNER) or
        (manager_can_create and spl.project.has_user(user, structure_models.ProjectRole.MANAGER))
    )


class OpenStackPackageCreateSerializer(openstack_serializers.TenantSerializer):
    template = serializers.HyperlinkedRelatedField(
        lookup_field='uuid',
        view_name='package-template-detail',
        write_only=True,
        queryset=models.PackageTemplate.objects.all())

    class Meta(openstack_serializers.TenantSerializer.Meta):
        fields = openstack_serializers.TenantSerializer.Meta.fields + ('template',)

    def validate_service_project_link(self, spl):
        # It should be possible for owner to create package but impossible to create a package directly.
        # So we need to ignore tenant spl validation.
        spl = super(openstack_serializers.TenantSerializer, self).validate_service_project_link(spl)

        user = self.context['request'].user
        if not _has_access_to_package(user, spl):
            raise serializers.ValidationError(_('You do not have permissions to create package for given project.'))
        return spl

    def validate_template(self, template):
        template = _check_template_service_settings(self, template)

        if template.archived:
            raise serializers.ValidationError(_('New package cannot be created for archived template.'))

        return template

    def validate(self, attrs):
        """ Additionally check that template and service project link belong to the same service settings """
        template = attrs['template']
        attrs = super(OpenStackPackageCreateSerializer, self).validate(attrs)
        spl = attrs['service_project_link']
        if spl.service.settings != template.service_settings:
            raise serializers.ValidationError(
                _('Template and service project link should be connected to the same service settings.'))
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        """ Create tenant and service settings from it """
        template = validated_data.pop('template')
        tenant = super(OpenStackPackageCreateSerializer, self).create(validated_data)
        _set_tenant_quotas(tenant, template)
        _set_tenant_extra_configuration(tenant, template)

        # service settings are created on tenant creation
        service_settings = structure_models.ServiceSettings.objects.get(
            scope=tenant,
            type=openstack_tenant_apps.OpenStackTenantConfig.service_name,
        )
        package = models.OpenStackPackage.objects.create(
            tenant=tenant,
            template=template,
            service_settings=service_settings,
        )
        return package


class OpenStackPackageSerializer(core_serializers.AugmentedSerializerMixin,
                                 serializers.HyperlinkedModelSerializer):
    name = serializers.CharField(source='tenant.name', read_only=True)
    description = serializers.CharField(source='tenant.description', read_only=True)

    class Meta(object):
        model = models.OpenStackPackage
        fields = ('url', 'uuid', 'name', 'description', 'template', 'tenant', 'service_settings',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'template': {'lookup_field': 'uuid', 'view_name': 'package-template-detail', 'read_only': True},
            'tenant': {'lookup_field': 'uuid', 'view_name': 'openstack-tenant-detail', 'read_only': True},
            'service_settings': {'lookup_field': 'uuid', 'read_only': True},
        }


class OpenStackPackageChangeSerializer(structure_serializers.PermissionFieldFilteringMixin, serializers.Serializer):
    package = serializers.HyperlinkedRelatedField(
        view_name='openstack-package-detail',
        lookup_field='uuid',
        queryset=models.OpenStackPackage.objects.all()
    )
    template = serializers.HyperlinkedRelatedField(
        view_name='package-template-detail',
        lookup_field='uuid',
        queryset=models.PackageTemplate.objects.all()
    )

    def get_filtered_field_names(self):
        return 'package',

    validate_template = _check_template_service_settings

    def validate_package(self, package):
        spl = package.tenant.service_project_link
        user = self.context['request'].user

        if package.tenant.state != openstack_models.Tenant.States.OK:
            raise serializers.ValidationError(_('Package\'s tenant must be in OK state.'))

        if not _has_access_to_package(user, spl):
            raise serializers.ValidationError(_('You do not have permissions to extend given package.'))

        return package

    def validate(self, attrs):
        package = attrs['package']
        new_template = attrs['template']
        if package.tenant.service_project_link.service.settings != new_template.service_settings:
            raise serializers.ValidationError(
                _('Template and package\'s tenant should be connected to the same service settings.'))

        if package.template == new_template:
            raise serializers.ValidationError(
                _('New package template cannot be the same as package\'s current template.'))

        usage = package.get_quota_usage()
        old_components = {component.type: component.amount for component in package.template.components.all()}
        for component in new_template.components.all():
            if component.type not in old_components:
                raise serializers.ValidationError(
                    _('Template\'s components must be the same as package template\'s components'))
            if component.type in usage and usage[component.type] > component.amount:
                msg = _("Current usage of {0} quota is greater than new template's {0} component.")
                raise serializers.ValidationError(msg.format(component.get_type_display()))
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        package = validated_data['package']
        new_template = validated_data['template']
        service_settings = package.service_settings

        tenant = package.tenant
        _set_tenant_quotas(tenant, new_template)
        _set_related_service_settings_quotas(tenant, new_template)
        _set_tenant_extra_configuration(tenant, new_template)

        package.delete()
        new_package = models.OpenStackPackage.objects.create(
            template=new_template,
            service_settings=service_settings,
            tenant=tenant
        )

        return new_package


class OpenStackPackageAssignSerializer(serializers.Serializer):
    template = serializers.HyperlinkedRelatedField(
        view_name='package-template-detail',
        lookup_field='uuid',
        write_only=True,
        queryset=models.PackageTemplate.objects.all()
    )
    tenant = serializers.HyperlinkedRelatedField(
        view_name='openstack-tenant-detail',
        lookup_field='uuid',
        write_only=True,
        queryset=openstack_models.Tenant.objects.all(),
    )

    def validate_template(self, template):
        template = _check_template_service_settings(self, template)

        if template.archived:
            raise serializers.ValidationError(_('Package cannot be assigned for archived template.'))

        return template

    def validate_tenant(self, tenant):
        if models.OpenStackPackage.objects.filter(tenant=tenant).exists():
            raise serializers.ValidationError(
                _('Package for tenant already exists. '
                  'Please use change package operation instead.'))
        return tenant

    def validate(self, attrs):
        attrs = super(OpenStackPackageAssignSerializer, self).validate(attrs)
        spl = attrs['tenant'].service_project_link
        template = attrs['template']
        if spl.service.settings != template.service_settings:
            raise serializers.ValidationError(
                _('Template and service project link should be connected to the same service settings.'))

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        tenant = validated_data['tenant']
        template = validated_data['template']
        _set_tenant_quotas(tenant, template)
        _set_tenant_extra_configuration(tenant, template)
        service_settings = structure_models.ServiceSettings.objects.get(
            scope=tenant,
            type=openstack_tenant_apps.OpenStackTenantConfig.service_name,
        )
        return models.OpenStackPackage.objects.create(
            tenant=tenant,
            template=template,
            service_settings=service_settings,
        )
