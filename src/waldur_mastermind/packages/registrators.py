from django.conf import settings

from waldur_core.structure.permissions import _get_project
from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.registrators import BaseRegistrator
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.packages import models as packages_models


class OpenStackItemRegistrator(BaseRegistrator):

    def get_customer(self, source):
        return source.tenant.service_project_link.project.customer

    def get_sources(self, customer):
        if not settings.WALDUR_PACKAGES['BILLING_ENABLED']:
            return packages_models.OpenStackPackage.objects.none()

        return packages_models.OpenStackPackage.objects.filter(
            tenant__service_project_link__project__customer=customer
        ).exclude(tenant__backend_id='').exclude(tenant__backend_id=None).distinct()

    def _create_item(self, source, invoice, start, end):
        package = source

        product_code = package.template.product_code
        article_code = package.template.article_code

        if package.template.unit == common_mixins.UnitPriceMixin.Units.PER_DAY:
            price = package.template.price
        else:
            price = package.template.monthly_price

        start = invoices_models.adjust_invoice_items(
            invoice, source, start, price, package.template.unit)

        item = invoices_models.InvoiceItem.objects.create(
            scope=package,
            project=_get_project(package),
            unit_price=price or 0,
            unit=package.template.unit,
            product_code=product_code,
            article_code=article_code,
            invoice=invoice,
            start=start,
            end=end,
            details=self.get_details(package))
        self.init_details(item)

    def get_details(self, source):
        package = source
        details = {
            'tenant_name': package.tenant.name,
            'tenant_uuid': package.tenant.uuid.hex,
            'template_name': package.template.name,
            'template_uuid': package.template.uuid.hex,
            'template_category': package.template.get_category_display(),
        }
        service_provider_info = marketplace_utils.get_service_provider_info(source)
        details.update(service_provider_info)
        return details

    def get_name(self, source):
        package = source
        template_category = package.template.get_category_display()
        tenant_name = package.tenant.name
        template_name = package.template.name

        if template_category:
            return '%s (%s / %s)' % (tenant_name, template_category, template_name)
        else:
            return '%s (%s)' % (tenant_name, template_name)
