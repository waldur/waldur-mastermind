import logging

from django.conf import settings
from django.db import transaction

from waldur_core.core import models as core_models
from waldur_core.core.utils import serialize_instance
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models, plugins
from waldur_mastermind.marketplace.utils import import_resource_metadata, format_list
from waldur_mastermind.marketplace_openstack import (
    INSTANCE_TYPE, VOLUME_TYPE, PACKAGE_TYPE, RAM_TYPE, STORAGE_TYPE
)
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


def create_offering_components(offering):
    fixed_components = plugins.manager.get_components(PACKAGE_TYPE)

    for component_data in fixed_components:
        marketplace_models.OfferingComponent.objects.create(
            offering=offering,
            **component_data._asdict()
        )


def copy_plan_components_from_template(plan, offering, template):
    component_map = {
        component.type: component
        for component in template.components.all()
    }

    for (key, component_data) in component_map.items():
        plan_component = component_map.get(key)
        offering_component = offering.components.get(type=key)

        amount = plan_component.amount
        price = plan_component.price

        # In marketplace RAM and storage is stored in GB, but in package plugin it is stored in MB.
        if key in (RAM_TYPE, STORAGE_TYPE):
            amount = int(amount / 1024)
            price = price * 1024

        marketplace_models.PlanComponent.objects.create(
            plan=plan,
            component=offering_component,
            amount=amount,
            price=price,
        )


def import_openstack_service_settings(default_customer, dry_run=False, require_templates=False):
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
        return 0, 0

    missing_templates = package_models.PackageTemplate.objects.filter(
        service_settings__in=missing_settings)

    settings_without_templates = missing_settings.exclude(
        id__in=missing_templates.values_list('service_settings_id', flat=True))

    def create_offering(service_settings, state):
        offering = marketplace_models.Offering.objects.create(
            scope=service_settings,
            type=PACKAGE_TYPE,
            name=service_settings.name,
            geolocations=service_settings.geolocations,
            customer=service_settings.customer or default_customer,
            category=category,
            shared=service_settings.shared,
            state=state,
        )
        create_offering_components(offering)
        return offering

    offerings_counter = 0
    plans_counter = 0

    if settings_without_templates.exists():
        logger.warning('The following service settings do not have package template, '
                       'therefore they would be imported in DRAFT state: %s',
                       format_list(settings_without_templates))

    if not require_templates:
        for service_settings in settings_without_templates:
            with transaction.atomic():
                create_offering(service_settings, marketplace_models.Offering.States.DRAFT)
                offerings_counter += 1

    for template in missing_templates:
        with transaction.atomic():
            service_settings = template.service_settings

            try:
                offering = marketplace_models.Offering.objects.get(scope=service_settings)
            except marketplace_models.Offering.DoesNotExist:
                offering = create_offering(service_settings, marketplace_models.Offering.States.ACTIVE)
                offerings_counter += 1

            plan = marketplace_models.Plan.objects.create(
                offering=offering,
                name=template.name,
                unit_price=template.price,
                unit=marketplace_models.Plan.Units.PER_DAY,
                product_code=template.product_code,
                article_code=template.article_code,
                scope=template,
            )
            plans_counter += 1

            copy_plan_components_from_template(plan, offering, template)

    return offerings_counter, plans_counter


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
        return 0

    packages = package_models.OpenStackPackage.objects.filter(tenant__in=missing_resources)
    tenants_without_packages = missing_resources.exclude(id__in=packages.values_list('tenant_id', flat=True))

    def create_resource(offering, tenant, plan=None):
        resource = marketplace_models.Resource.objects.create(
            name=tenant.name,
            created=tenant.created,
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
        if plan and tenant.backend_id:
            marketplace_models.ResourcePlanPeriod.objects.create(
                resource=resource,
                plan=plan,
                start=tenant.created,
            )
        import_resource_metadata(resource)
        return resource

    resource_counter = 0
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
        resource_counter += 1

    for package in packages:
        tenant = package.tenant
        try:
            offering = marketplace_models.Offering.objects.get(scope=tenant.service_settings)
            plan = marketplace_models.Plan.objects.get(scope=package.template, offering=offering)
        except marketplace_models.Plan.DoesNotExist:
            logger.warning('Plan for template is not imported yet. '
                           'Template ID: %s.', package.template_id)
            continue

        create_resource(plan.offering, tenant, plan)
        resource_counter += 1

    return resource_counter


def import_openstack_tenant_service_settings(dry_run=False):
    """
    Import OpenStack tenant service settings as marketplace offerings.
    """

    offerings_counter = 0
    plans_counter = 0

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
                type=offering_type,
                state=marketplace_models.Offering.States.ACTIVE,
                billable=False,
            )
            create_offering_components(offering)
            offerings_counter += 1

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
                scope=parent_plan.scope
            )

            copy_plan_components_from_template(plan, offering, template)
            plans_counter += 1

    return offerings_counter, plans_counter


def get_plan_for_resource(resource, offering):
    tenant = resource.service_settings.scope
    if not tenant:
        logger.warning('Skipping billing for resource because it does not have shared OpenStack settings. '
                       'Resource: %s', serialize_instance(resource))
        return

    try:
        package = package_models.OpenStackPackage.objects.get(tenant=tenant)
    except package_models.OpenStackPackage.DoesNotExist:
        logger.warning('Skipping billing for resource because package for tenant is not defined. '
                       'Tenant ID: %s', tenant.id)
        return

    try:
        plan = marketplace_models.Plan.objects.get(scope=package.template, offering=offering)
    except marketplace_models.Plan.DoesNotExist:
        logger.warning('Skipping billing for resource because plan for template is not defined. '
                       'Template ID: %s', package.template)
        return

    return plan


def import_openstack_instances_and_volumes(dry_run=False):
    """
    Import OpenStack tenant resources as marketplace resources.
    It is expected that offerings for OpenStack tenant service settings are imported before this command is ran.
    """
    model_classes = {
        INSTANCE_TYPE: openstack_tenant_models.Instance,
        VOLUME_TYPE: openstack_tenant_models.Volume,
    }

    resources_counter = 0

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

        for resource in missing_resources:
            offering = offerings.get(resource.service_settings)
            if not offering:
                logger.warning('Offering for service setting with ID %s is not imported yet.',
                               resource.service_settings.id)
                continue

            plan = get_plan_for_resource(resource, offering)

            new_resource = marketplace_models.Resource.objects.create(
                name=resource.name,
                created=resource.created,
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
            if isinstance(resource, openstack_tenant_models.Volume):
                import_volume_metadata(new_resource)
            if isinstance(resource, openstack_tenant_models.Instance):
                import_instance_metadata(new_resource)
            resources_counter += 1

    return resources_counter


def import_volume_metadata(resource):
    import_resource_metadata(resource)
    volume = resource.scope
    resource.backend_metadata['size'] = volume.size

    if volume.instance:
        resource.backend_metadata['instance_uuid'] = volume.instance.uuid.hex
        resource.backend_metadata['instance_name'] = volume.instance.name
    else:
        resource.backend_metadata['instance_uuid'] = None
        resource.backend_metadata['instance_name'] = None

    resource.save(update_fields=['backend_metadata'])


def import_instance_metadata(resource):
    import_resource_metadata(resource)
    instance = resource.scope
    resource.backend_metadata['internal_ips'] = instance.internal_ips
    resource.backend_metadata['external_ips'] = instance.external_ips
    resource.save(update_fields=['backend_metadata'])
