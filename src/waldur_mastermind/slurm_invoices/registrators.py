import logging
import math

from waldur_mastermind.invoices import registrators
from waldur_mastermind.invoices import models as invoice_models

from waldur_slurm import models as slurm_models
from . import models

logger = logging.getLogger(__name__)


class AllocationRegistrator(registrators.BaseRegistrator):
    def get_sources(self, customer):
        return slurm_models.Allocation.objects.filter(service_project_link__project__customer=customer).distinct()

    def has_sources(self, customer):
        return self.get_sources(customer).exists()

    def get_customer(self, source):
        return source.service_project_link.project.customer

    def _find_item(self, source, now):
        allocation = source
        result = invoice_models.GenericInvoiceItem.objects.filter(
            scope=allocation,
            invoice__customer=allocation.service_project_link.project.customer,
            invoice__state=invoice_models.Invoice.States.PENDING,
            invoice__year=now.year,
            invoice__month=now.month,
        ).first()
        return result

    def _create_item(self, source, invoice, start, end):
        allocation = source
        package = self.get_package(allocation)
        if package:
            return invoice_models.GenericInvoiceItem.objects.create(
                scope=allocation,
                project=source.service_project_link.project,
                unit_price=self.get_price(allocation, package),
                unit=invoice_models.GenericInvoiceItem.Units.QUANTITY,
                quantity=1,
                product_code=package.product_code,
                article_code=package.article_code,
                invoice=invoice,
                start=start,
                end=end,
            )

    def get_package(self, allocation):
        service_settings = allocation.service_project_link.service.settings
        try:
            return models.SlurmPackage.objects.get(service_settings=service_settings)
        except models.SlurmPackage.DoesNotExist:
            logger.debug('Skip SLURM invoice item because pricing package'
                         ' for service settings %s is not defined.', service_settings)

    def get_price(self, allocation, package):
        minutes_in_hour = 60
        mb_in_gb = 1024
        cpu_price = int(math.ceil(1.0 * allocation.cpu_usage / minutes_in_hour)) * package.cpu_price
        gpu_price = int(math.ceil(1.0 * allocation.gpu_usage / minutes_in_hour)) * package.gpu_price
        ram_price = int(math.ceil(1.0 * allocation.ram_usage / mb_in_gb)) * package.ram_price
        return cpu_price + gpu_price + ram_price

    def get_details(self, source):
        return {
            'cpu_usage': source.cpu_usage,
            'gpu_usage': source.gpu_usage,
            'ram_usage': source.ram_usage,
        }
