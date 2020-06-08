import logging

from django.contrib.contenttypes.models import ContentType

from waldur_core.core import utils as core_utils
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import registrators
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
from waldur_slurm import models as slurm_models

from . import models, utils

logger = logging.getLogger(__name__)


class AllocationRegistrator(registrators.BaseRegistrator):
    def get_sources(self, customer):
        return slurm_models.Allocation.objects.filter(
            service_project_link__project__customer=customer
        ).distinct()

    def get_customer(self, source):
        return source.service_project_link.project.customer

    def _find_item(self, source, now):
        """
        Find a list of items by source and date.
        :param source: object that was bought by customer.
        :param now: date of invoice with invoice items.
        :return: list of invoice items related to allocation (source)
        """
        model_type = ContentType.objects.get_for_model(source)
        result = invoice_models.InvoiceItem.objects.filter(
            content_type=model_type,
            object_id=source.id,
            invoice__customer=self.get_customer(source),
            invoice__state=invoice_models.Invoice.States.PENDING,
            invoice__year=now.year,
            invoice__month=now.month,
            end=core_utils.month_end(now),
        )
        return result

    def _create_item(self, source, invoice, start, end):
        allocation = source
        package = self.get_package(allocation)
        if package:
            allocation_usage = slurm_models.AllocationUsage.objects.filter(
                allocation=allocation, month=start.month, year=start.year
            ).first()

            if not allocation_usage:
                return

            for component in manager.get_components(PLUGIN_NAME):
                component_type = component.type
                component_usage = getattr(allocation_usage, component_type + '_usage')
                if component_usage > 0:
                    item = invoice_models.InvoiceItem.objects.create(
                        scope=allocation,
                        project=source.service_project_link.project,
                        unit_price=utils.get_unit_deposit_usage(
                            allocation_usage, package, component_type
                        ),
                        unit=invoice_models.InvoiceItem.Units.QUANTITY,
                        quantity=component_usage,
                        product_code=package.product_code,
                        article_code=package.article_code,
                        invoice=invoice,
                        start=start,
                        end=end,
                    )
                    item.name = '%s (%s)' % (self.get_name(item.scope), component.name)
                    details = self.get_details(source)
                    details.update({'type': component_type})
                    item.details.update(details)
                    item.save(update_fields=['name', 'details'])

    def get_package(self, allocation):
        service_settings = allocation.service_project_link.service.settings
        try:
            return models.SlurmPackage.objects.get(service_settings=service_settings)
        except models.SlurmPackage.DoesNotExist:
            logger.debug(
                'Skip SLURM invoice item because pricing package'
                ' for service settings %s is not defined.',
                service_settings,
            )

    def get_details(self, source):
        details = {
            'scope_uuid': source.uuid.hex,
        }
        service_provider_info = marketplace_utils.get_service_provider_info(source)
        details.update(service_provider_info)
        return details

    def get_component_details(self, offering, plan_component):
        details = self.get_details(offering)
        details.update(
            {
                'plan_component_id': plan_component.id,
                'offering_component_type': plan_component.component.type,
                'offering_component_name': plan_component.component.name,
                'offering_component_measured_unit': plan_component.component.measured_unit,
            }
        )
        return details
