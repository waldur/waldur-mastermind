from __future__ import unicode_literals

""" Defines how to optimize OpenStack packages """
from rest_framework import serializers as rf_serializers

from waldur_cost_planning import optimizers, register, serializers
from waldur_openstack.openstack import apps as openstack_apps

from waldur_mastermind.packages.models import PackageTemplate, PackageComponent
from waldur_mastermind.packages.serializers import PackageTemplateSerializer


OptimizedOpenStack = optimizers.namedtuple_with_defaults(
    'OptimizedOpenStack',
    field_names=optimizers.OptimizedService._fields + ('package_template',),
    default_values=optimizers.OptimizedService._defaults,
)


class OpenStackOptimizer(optimizers.Optimizer):
    """ Find the cheapest package template for OpenStack service """

    def optimize(self, deployment_plan, service):
        requirements = deployment_plan.get_requirements()
        # Step 1. Find suitable templates.
        templates = PackageTemplate.objects.filter(
            service_settings=service.settings).prefetch_related('components')
        suitable_templates = []
        for template in templates:
            components = {c.type: c.amount for c in template.components.all()}
            is_suitable = (
                components[PackageComponent.Types.RAM] >= requirements['ram'] and
                components[PackageComponent.Types.STORAGE] >= requirements['storage'] and
                components[PackageComponent.Types.CORES] >= requirements['cores']
            )
            if is_suitable:
                suitable_templates.append(template)
        # Step 2. Find the cheapest template.
        if not suitable_templates:
            raise optimizers.OptimizationError('There is no package template that can hold the deployment plan.')
        cheapest_template = min(suitable_templates, key=lambda t: t.price)
        return OptimizedOpenStack(service, cheapest_template.price, '', cheapest_template)


register.Register.register_optimizer(openstack_apps.OpenStackConfig.service_name, OpenStackOptimizer)


class OptimizedOpenStackSerializer(serializers.OptimizedServiceSerializer):
    service = rf_serializers.HyperlinkedRelatedField(
        view_name='openstack-detail',
        lookup_field='uuid',
        read_only=True,
    )

    def get_fields(self):
        fields = super(OptimizedOpenStackSerializer, self).get_fields()
        fields['package_template'] = PackageTemplateSerializer(read_only=True)
        return fields


register.Register.register_serializer(openstack_apps.OpenStackConfig.service_name, OptimizedOpenStackSerializer)
