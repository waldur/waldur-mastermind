import logging

from waldur_mastermind.invoices import registrators
from waldur_mastermind.invoices import models as invoice_models

from . import models

logger = logging.getLogger(__name__)


class ExpertRequestRegistrator(registrators.BaseRegistrator):
    def get_sources(self, customer):
        return models.ExpertRequest.objects.filter(project__customer=customer,
                                                   state=models.ExpertRequest.States.COMPLETED)

    def get_customer(self, source):
        return source.customer

    def _find_item(self, source, now):
        expert_request = source
        result = invoice_models.GenericInvoiceItem.objects.filter(
            scope=expert_request,
            invoice__customer=self.get_customer(expert_request),
            invoice__state=invoice_models.Invoice.States.PENDING,
            invoice__year=now.year,
            invoice__month=now.month,
        ).first()

        return result

    def _create_item(self, source, invoice, start, end):
        expert_request = source

        if (not expert_request.recurring_billing and
                invoice_models.GenericInvoiceItem.objects.filter(scope=expert_request).exists()):
            return

        return invoice_models.GenericInvoiceItem.objects.create(
            scope=expert_request,
            project=expert_request.project,
            unit_price=self.get_price(expert_request),
            unit=invoice_models.GenericInvoiceItem.Units.QUANTITY,
            quantity=1,
            product_code=expert_request.product_code,
            article_code=expert_request.article_code,
            invoice=invoice,
            start=start,
            end=end,
        )

    def get_price(self, expert_request):
        return expert_request.contract.price

    def get_details(self, source):
        expert_request = source

        return {
            'expert_request_type': expert_request.type,
            'expert_request_recurring': expert_request.recurring_billing,
            'expert_request_name': expert_request.name,
            'issue_summary': expert_request.issue.summary,
            'issue_key': expert_request.issue.key,
            'issue_link': expert_request.issue.link,
        }
