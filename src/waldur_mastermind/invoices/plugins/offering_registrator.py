from waldur_mastermind.invoices import models
from waldur_mastermind.invoices.registrators import BaseRegistrator
from waldur_mastermind.support import models as support_models


class OfferingItemRegistrator(BaseRegistrator):

    def get_sources(self, customer):
        return support_models.Offering.objects.filter(project__customer=customer).distinct()

    def has_sources(self, customer):
        return self.get_sources(customer).exists()

    def get_customer(self, source):
        return source.project.customer

    def _find_item(self, source, now):
        offering = source
        result = models.OfferingItem.objects.filter(
            offering=offering,
            invoice__customer=offering.project.customer,
            invoice__state=models.Invoice.States.PENDING,
            invoice__year=now.year,
            invoice__month=now.month,
        ).first()
        return result

    def _create_item(self, source, invoice, start, end):
        offering = source
        result = models.OfferingItem.objects.create(
            offering=offering,
            project=offering.project,
            unit_price=offering.unit_price,
            unit=offering.unit,
            product_code=offering.product_code,
            article_code=offering.article_code,
            invoice=invoice,
            start=start,
            end=end,
        )
        return result
