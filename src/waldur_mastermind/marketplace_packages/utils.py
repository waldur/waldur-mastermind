import logging

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist

from waldur_mastermind.packages import models as package_models
from waldur_mastermind.marketplace import models as marketplace_models


logger = logging.getLogger(__name__)


def create_offering_for_package_template(template):
    conf = settings.WALDUR_MARKETPLACE_PACKAGES
    customer_id = conf.get('CUSTOMER_ID')
    category_id = conf.get('CATEGORY_ID')

    if not customer_id:
        logger.debug('Marketplace offering for VPC is not '
                     'created because customer ID is not defined.')
        return

    if not category_id:
        logger.debug('Marketplace offering for VPC is not '
                     'created because category ID is not defined.')
        return

    attributes = {
        component.type: component.amount
        for component in template.components.all()
    }
    marketplace_models.Offering.objects.create(
        name=template.name,
        description=template.description,
        customer_id=customer_id,
        category_id=category_id,
        geolocations=template.service_settings.geolocations,
        attributes=attributes,
        scope=template,
    )


def update_offering_for_template(template):
    fields = {}
    changed = set(template.tracker.changed())
    if 'name' in changed:
        fields['name'] = template.name
    if 'description' in changed:
        fields['description'] = template.description
    if 'archived' in changed:
        fields['is_active'] = not template.archived
    if fields:
        marketplace_models.Offering.objects.filter(scope=template).update(**fields)


def sync_offering_attribute_with_template_component(component):
    try:
        offering = marketplace_models.Offering.objects.get(scope=component.template)
    except ObjectDoesNotExist:
        logger.debug('Skipping offering attributes synchronization because offering is not found.')
    else:
        offering.attributes[component.type] = component.amount
        offering.save(update_fields=['attributes'])


def create_missing_offerings():
    content_type = ContentType.objects.get_for_model(package_models.PackageTemplate)
    offerings = marketplace_models.Offering.objects.filter(content_type=content_type)
    front_templates = set(offerings.values_list('object_id', flat=True))
    back_templates = set(package_models.PackageTemplate.objects.all().values_list('pk', flat=True))
    missing_ids = back_templates - front_templates

    missing_templates = package_models.PackageTemplate.objects.filter(pk__in=missing_ids)
    for template in missing_templates:
        create_offering_for_package_template(template)
