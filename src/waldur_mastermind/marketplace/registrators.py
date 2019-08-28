from waldur_mastermind.invoices.registrators import BaseRegistrator
from waldur_mastermind.invoices import models as invoices_models

from . import models


class MarketplaceItemRegistrator(BaseRegistrator):
    def get_customer(self, source):
        return source.project.customer

    def get_sources(self, customer):
        return models.Resource.objects.filter(
            project__customer=customer
        ).exclude(backend_id=None).exclude(backend_id='').distinct()

    def _create_item(self, source, invoice, start, end):
        details = self.get_details(source)
        invoices_models.GenericInvoiceItem.objects.create(
            scope=source,
            project=source.project,
            unit_price=resource.unit_price,
            unit=resource.plan.unit,
            product_code=resource.plan.product_code,
            article_code=resource.plan.article_code,
            invoice=invoice,
            start=start,
            end=end,
            details=details,
        )
