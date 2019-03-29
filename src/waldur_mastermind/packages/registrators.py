from django.utils import timezone

from waldur_core.structure.permissions import _get_project
from waldur_mastermind.invoices import models as invoices_models, utils as invoices_utils
from waldur_mastermind.invoices.registrators import BaseRegistrator
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.packages import models as packages_models

from . import utils


class OpenStackItemRegistrator(BaseRegistrator):

    def get_customer(self, source):
        return source.tenant.service_project_link.project.customer

    def get_sources(self, customer):
        return packages_models.OpenStackPackage.objects.filter(
            tenant__service_project_link__project__customer=customer).distinct()

    def _find_item(self, source, now):
        result = utils.get_openstack_items().filter(
            object_id=source.id,
            invoice__customer=self.get_customer(source),
            invoice__state=invoices_models.Invoice.States.PENDING,
            invoice__year=now.year,
            invoice__month=now.month,
        ).first()
        return result

    def _create_item(self, source, invoice, start, end):
        package = source
        overlapping_item = utils.get_openstack_items().filter(
            invoice=invoice,
            end__day=start.day,
            details__tenant_name=package.tenant.name,
        ).order_by('-unit_price').first()

        daily_price = package.template.price
        product_code = package.template.product_code
        article_code = package.template.article_code
        if overlapping_item:
            """
            Notes:
            |- date -| - used during the date
            |- **** -| - used during the day
            |- ---- -| - was requested to use in the current day but will be moved to next or previous one.
            |-***?---| - was used for a half day and '?' stands for a conflict.

            If there is an item that overlaps with current one as shown below:
            |--03.01.2017-|-********-|-***?---|
                                     |----?**-|-06.01.2017-|-******-|
            we have to make next steps:
            1) If item is more expensive -> use it for price calculation
                and register new package starting from next day [-06.01.2017-]
            |--03.01.2017-|-********-|-*****-|
                                     |-------|-06.01.2017-|-******-|

            2) If old package item is more expensive and it is the end of the month
            extend package usage till the end of the day and set current package end date to start date,
            so that usage days is 0 but it is still registered in the invoice.
            |--29.01.2017-|-********-|-***31.01.2017***-|
                                     |----31.01.2017----|

            3) If item is cheaper do exactly the opposite and shift its end date to yesterday,
            so new package will be registered today
            |--03.01.2017-|-********-|-------|
                                     |-*****-|-06.01.2017-|-******-|
            """
            if overlapping_item.unit_price > daily_price:
                if overlapping_item.end.day == invoices_utils.get_current_month_end().day:
                    utils.extend_to_the_end_of_the_day(overlapping_item)
                    end = start
                else:
                    start = start + timezone.timedelta(days=1)
            else:
                utils.shift_backward(overlapping_item)

        invoices_models.GenericInvoiceItem.objects.create(
            scope=package,
            project=_get_project(package),
            unit_price=daily_price,
            unit=invoices_models.GenericInvoiceItem.Units.PER_DAY,
            product_code=product_code,
            article_code=article_code,
            invoice=invoice,
            start=start,
            end=end,
            details=self.get_details(package))

    def get_details(self, source):
        package = source
        details = {
            'name': utils.get_invoice_item_name(package),
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
        return utils.get_invoice_item_name(source)

    def terminate(self, source, now=None):
        super(OpenStackItemRegistrator, self).terminate(source, now)
        package = source
        utils.get_openstack_items().filter(object_id=package.id).update(object_id=None)
