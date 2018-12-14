import logging

from django.conf import settings

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models, plugins
from waldur_mastermind.marketplace_openstack import INSTANCE_TYPE, VOLUME_TYPE, PACKAGE_TYPE
from waldur_mastermind.packages import models as package_models
from waldur_openstack.openstack import apps as openstack_apps
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack_tenant import apps as openstack_tenant_apps
from waldur_openstack.openstack_tenant import models as openstack_tenant_models

logger = logging.getLogger(__name__)


def get_offering_category_for_tenant():
    return marketplace_models.Category.objects.get(
        uuid=settings.WALDUR_MARKETPLACE_OPENSTACK['TENANT_CATEGORY_UUID']
    )


def get_offering_name_for_instance(tenant):
    return 'Virtual machine in %s' % tenant.name


def get_offering_category_for_instance():
    return marketplace_models.Category.objects.get(
        uuid=settings.WALDUR_MARKETPLACE_OPENSTACK['INSTANCE_CATEGORY_UUID']
    )


def get_offering_name_for_volume(tenant):
    return 'Volume in %s' % tenant.name


def get_offering_category_for_volume():
    return marketplace_models.Category.objects.get(
        uuid=settings.WALDUR_MARKETPLACE_OPENSTACK['VOLUME_CATEGORY_UUID']
    )


def get_category_and_name_for_offering_type(offering_type, service_settings):
    if offering_type == INSTANCE_TYPE:
        category = get_offering_category_for_instance()
        name = get_offering_name_for_instance(service_settings)
        return category, name
    elif offering_type == VOLUME_TYPE:
        category = get_offering_category_for_volume()
        name = get_offering_name_for_volume(service_settings)
        return category, name


def get_resource_state(state):
    SrcStates = core_models.StateMixin.States
    DstStates = marketplace_models.Resource.States
    mapping = {
        SrcStates.CREATION_SCHEDULED: DstStates.CREATING,
        SrcStates.CREATING: DstStates.CREATING,
        SrcStates.UPDATE_SCHEDULED: DstStates.UPDATING,
        SrcStates.UPDATING: DstStates.UPDATING,
        SrcStates.DELETION_SCHEDULED: DstStates.TERMINATING,
        SrcStates.DELETING: DstStates.TERMINATING,
        SrcStates.OK: DstStates.OK,
        SrcStates.ERRED: DstStates.ERRED,
    }
    return mapping.get(state, DstStates.ERRED)


def format_list(resources):
    """
    Format comma-separated list of IDs from Django queryset.
    """
    return ', '.join(map(str, resources.values_list('id', flat=True)))


def copy_components(plan, offering, fixed_components, component_map):
    for component_data in fixed_components:
        offering_component = marketplace_models.OfferingComponent.objects.create(
            offering=offering,
            **component_data._asdict()
        )

        plan_component = component_map.get(offering_component.type)
        if not plan_component:
            logger.warning('Skipping component because it is not found. '
                           'Offering ID: %s, component type: %s.',
                           offering.id, offering_component.type)
            continue

        marketplace_models.PlanComponent.objects.create(
            plan=plan,
            component=offering_component,
            amount=plan_component.amount,
            price=plan_component.price,
        )


def import_openstack_service_settings(default_customer, dry_run=False):
    """
    Import OpenStack service settings as marketplace offerings.
    """
    service_type = openstack_apps.OpenStackConfig.service_name
    category = get_offering_category_for_tenant()

    package_offerings = marketplace_models.Offering.objects.filter(type=PACKAGE_TYPE)
    front_settings = set(package_offerings.exclude(object_id=None).values_list('object_id', flat=True))

    back_settings = structure_models.ServiceSettings.objects.filter(type=service_type)
    missing_settings = back_settings.exclude(id__in=front_settings)

    if dry_run:
        logger.warning('OpenStack service settings would be imported to marketplace. '
                       'ID: %s.', format_list(missing_settings))
        return

    missing_templates = package_models.PackageTemplate.objects.filter(
        service_settings__in=missing_settings)

    settings_without_templates = missing_settings.exclude(
        id__in=missing_templates.values_list('service_settings_id', flat=True))

    def create_offering(service_settings, state=marketplace_models.Offering.States.ACTIVE):
        return marketplace_models.Offering.objects.create(
            scope=service_settings,
            type=PACKAGE_TYPE,
            name=service_settings.name,
            geolocations=service_settings.geolocations,
            customer=service_settings.customer or default_customer,
            category=category,
            shared=service_settings.shared,
            state=state,
        )

    for service_settings in settings_without_templates:
        create_offering(service_settings)

    for template in missing_templates:
        service_settings = template.service_settings

        offering_state = marketplace_models.Offering.States.ACTIVE
        if template.archived:
            offering_state = marketplace_models.Offering.States.ARCHIVED

        try:
            offering = marketplace_models.Offering.objects.get(scope=service_settings)
        except marketplace_models.Offering.DoesNotExist:
            offering = create_offering(service_settings, offering_state)

        plan = marketplace_models.Plan.objects.create(
            offering=offering,
            name=template.name,
            unit_price=template.price,
            unit=marketplace_models.Plan.Units.PER_DAY,
            product_code=template.product_code,
            article_code=template.article_code,
        )
        plan.scope = template
        plan.save()

        component_map = {
            component.type: component
            for component in template.components.all()
        }
        fixed_components = plugins.manager.get_components(offering.type)

        copy_components(plan, offering, fixed_components, component_map)


def import_openstack_tenants(dry_run=False):
    """
    Import OpenStack tenants as marketplace resources.
    It is expected that offerings for OpenStack service settings are imported before this command is ran.
    """
    front_ids = set(marketplace_models.Resource.objects.
                    filter(offering__type=PACKAGE_TYPE).
                    values_list('object_id', flat=True))
    missing_resources = openstack_models.Tenant.objects.exclude(id__in=front_ids)

    if dry_run:
        logger.warning('OpenStack tenants would be imported to marketplace. '
                       'ID: %s.', format_list(missing_resources))
        return

    packages = package_models.OpenStackPackage.objects.filter(tenant__in=missing_resources)
    tenants_without_packages = missing_resources.exclude(id__in=packages.values_list('tenant_id', flat=True))

    def create_resource(offering, tenant, plan=None):
        return marketplace_models.Resource.objects.create(
            offering=offering,
            plan=plan,
            scope=tenant,
            project=tenant.project,
            state=get_resource_state(tenant.state),
            attributes=dict(
                name=tenant.name,
                description=tenant.description,
                user_username=tenant.user_username,
                user_password=tenant.user_password,
            )
        )

    for tenant in tenants_without_packages:
        # It is expected that service setting has exactly one offering
        # if it does not have package
        try:
            offering = marketplace_models.Offering.objects.get(scope=tenant.service_settings)
        except marketplace_models.Offering.DoesNotExist:
            logger.warning('Offering for service setting is not imported yet. '
                           'Service setting ID: %s.', tenant.service_settings.id)
            continue

        create_resource(offering, tenant)

    for package in packages:
        tenant = package.tenant
        try:
            plan = marketplace_models.Plan.objects.get(scope=package.template)
        except marketplace_models.Plan.DoesNotExist:
            logger.warning('Plan for template is not imported yet. '
                           'Template ID: %s.', package.template_id)
            continue

        create_resource(plan.offering, tenant, plan)


def import_openstack_tenant_service_settings(dry_run=False):
    """
    Import OpenStack tenant service settings as marketplace offerings.
    """

    for offering_type in (INSTANCE_TYPE, VOLUME_TYPE):
        marketplace_offerings = marketplace_models.Offering.objects.filter(type=offering_type)
        front_settings = set(marketplace_offerings.exclude(object_id=None).values_list('object_id', flat=True))
        missing_settings = structure_models.ServiceSettings.objects.filter(
            type=openstack_tenant_apps.OpenStackTenantConfig.service_name
        ).exclude(id__in=front_settings)

        if dry_run:
            logger.warning('OpenStack tenant service settings would be imported to marketplace. '
                           'ID: %s.', format_list(missing_settings))
            continue

        packages = package_models.OpenStackPackage.objects.filter(service_settings__in=missing_settings)
        settings_to_template = {package.service_settings: package.template for package in packages}

        for service_settings in missing_settings:
            category, offering_name = get_category_and_name_for_offering_type(offering_type, service_settings)
            offering = marketplace_models.Offering.objects.create(
                customer=service_settings.customer,
                category=category,
                name=offering_name,
                scope=service_settings,
                shared=service_settings.shared,
                type=offering_type
            )

            template = settings_to_template.get(service_settings)
            if not template:
                logger.warning('Billing for service setting is not imported because it does not have template. '
                               'Service setting ID: %s', service_settings.id)
                continue

            try:
                parent_plan = marketplace_models.Plan.objects.get(scope=template, offering__type=PACKAGE_TYPE)
            except marketplace_models.Plan.DoesNotExist:
                logger.warning('Billing for template is not imported because it does not have plan. '
                               'Template ID: %s', template.id)
                continue

            plan = marketplace_models.Plan.objects.create(
                offering=offering,
                name=parent_plan.name,
                scope=parent_plan.scope,
            )

            component_map = {
                component.type: component
                for component in template.components.all()
            }

            fixed_components = plugins.manager.get_components(parent_plan.offering.type)
            copy_components(plan, offering, fixed_components, component_map)


def import_openstack_instances_and_volumes(dry_run=False):
    """
    Import OpenStack tenant resources as marketplace resources.
    It is expected that offerings for OpenStack tenant service settings are imported before this command is ran.
    """
    model_classes = {
        INSTANCE_TYPE: openstack_tenant_models.Instance,
        VOLUME_TYPE: openstack_tenant_models.Volume,
    }

    for offering_type in (INSTANCE_TYPE, VOLUME_TYPE):
        front_ids = set(marketplace_models.Resource.objects.
                        filter(offering__type=offering_type).
                        values_list('object_id', flat=True))

        model_class = model_classes[offering_type]
        missing_resources = model_class.objects.exclude(id__in=front_ids)

        if dry_run:
            ids = format_list(missing_resources)
            logger.warning('OpenStack resource with IDs would be imported to marketplace: %s.', ids)
            continue

        offerings = {
            offering.scope: offering
            for offering in marketplace_models.Offering.objects.filter(type=offering_type)
        }

        templates = {
            package.tenant: package.template
            for package in package_models.OpenStackPackage.objects.all()
        }

        plans = {
            plan.scope: plan
            for plan in marketplace_models.Plan.objects.filter(offering__type=offering_type)
            if plan.scope
        }

        for resource in missing_resources:
            offering = offerings.get(resource.service_settings)
            if not offering:
                logger.warning('Offering for service setting with ID %s is not imported yet.',
                               resource.service_settings.id)
                continue

            tenant = resource.service_settings.scope
            template = templates.get(tenant)
            plan = plans.get(template)

            if plan and plan.offering != offering:
                logger.warning('Marketplace plan is not valid because it refers to another offering. '
                               'Plan ID: %s, offering ID: %s', plan.id, offering.id)

            marketplace_models.Resource.objects.create(
                project=resource.project,
                offering=offering,
                plan=plan,
                scope=resource,
                state=get_resource_state(resource.state),
                attributes=dict(
                    name=resource.name,
                    description=resource.description,
                ),
            )
