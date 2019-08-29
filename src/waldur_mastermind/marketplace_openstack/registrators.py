from waldur_mastermind.invoices.registrators import BaseRegistrator
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace_openstack import PACKAGE_TYPE

from waldur_mastermind.marketplace import models


class MarketplaceItemRegistrator(BaseRegistrator):
    def get_customer(self, source):
        return source.project.customer

    def get_sources(self, customer):
        return models.Resource.objects.filter(
            project__customer=customer,
            offering__type=PACKAGE_TYPE
        ).exclude(state__in=[
            models.Resource.States.CREATING,
            models.Resource.States.TERMINATED
        ])

    def _create_item(self, source, invoice, start, end):
        details = self.get_details(source)
        unit_price = sum(
            source.limits[component.component.type] * component.price
            for component in source.plan.components.all()
        )
        invoices_models.GenericInvoiceItem.objects.create(
            scope=source,
            project=source.project,
            unit_price=unit_price,
            unit=source.plan.unit,
            product_code=source.plan.product_code,
            article_code=source.plan.article_code,
            invoice=invoice,
            start=start,
            end=end,
            details=details,
        )
