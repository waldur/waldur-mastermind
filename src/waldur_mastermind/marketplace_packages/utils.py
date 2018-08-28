import logging

from django.conf import settings
from django.db import transaction

from waldur_mastermind.packages import models as package_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_packages import PLUGIN_NAME


logger = logging.getLogger(__name__)


def create_offering_and_plan_for_package_template(template):
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

    service_settings = template.service_settings

    with transaction.atomic():
        defaults = dict(
            name=service_settings.name,
            geolocations=service_settings.geolocations,
            customer_id=customer_id,
            category_id=category_id,
        )
        offering, _ = marketplace_models.Offering.objects.get_or_create(
            scope=service_settings,
            type=PLUGIN_NAME,
            defaults=defaults,
        )
        marketplace_models.Plan.objects.create(
            scope=template,
            offering=offering,
            name=template.name,
            unit_price=template.price,
            unit=marketplace_models.Plan.Units.PER_DAY,
            product_code=template.product_code,
            article_code=template.article_code,
        )


def update_related_object(scope, related_model, field_names):
    fields = {}
    changed = set(scope.tracker.changed())
    for field in field_names:
        if field in changed:
            fields[field] = getattr(scope, field)
    if fields:
        related_model.objects.filter(scope=scope).update(**fields)


def update_offering_for_service_settings(service_settings):
    update_related_object(
        service_settings,
        marketplace_models.Offering,
        ('name', 'geolocations')
    )


def update_plan_for_template(template):
    update_related_object(
        template,
        marketplace_models.Plan,
        ('name', 'archived', 'product_code', 'article_code')
    )


def create_missing_offerings():
    offerings = marketplace_models.Offering.objects.filter(type=PLUGIN_NAME)
    front_settings = set(offerings.exclude(object_id=None).values_list('object_id', flat=True))
    back_settings = set(package_models.PackageTemplate.objects.all().values_list('service_settings_id', flat=True))
    missing_ids = back_settings - front_settings

    missing_templates = package_models.PackageTemplate.objects.filter(service_settings__in=missing_ids)
    for template in missing_templates:
        create_offering_and_plan_for_package_template(template)
