from datetime import timedelta
import logging

from waldur_core.structure.permissions import _get_project
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.registrators import BaseRegistrator
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_vmware import models as vmware_models

from . import utils

logger = logging.getLogger(__name__)


class VirtualMachineRegistrator(BaseRegistrator):

    def get_customer(self, source):
        return source.service_project_link.project.customer

    def get_sources(self, customer):
        return vmware_models.VirtualMachine.objects.filter(
            service_project_link__project__customer=customer
        ).exclude(backend_id=None).exclude(backend_id='').distinct()

    def _find_item(self, source, now):
        result = utils.get_vm_items().filter(
            object_id=source.id,
            invoice__customer=self.get_customer(source),
            invoice__state=invoices_models.Invoice.States.PENDING,
            invoice__year=now.year,
            invoice__month=now.month,
        ).first()
        return result

    def _create_item(self, source, invoice, start, end):
        try:
            resource = marketplace_models.Resource.objects.get(scope=source)
            plan = resource.plan
            if not plan:
                logger.warning('Skipping VMware item invoice creation because '
                               'billing plan is not defined for resource. '
                               'Resource ID: %s', resource.id)
                return
        except marketplace_models.Resource.DoesNotExist:
            logger.warning('Skipping VMware item invoice creation because '
                           'marketplace resource is not available for VMware resource. '
                           'Resource ID: %s', source.id)
            return

        components_map = {
            plan_component.component.type: plan_component.price
            for plan_component in plan.components.all()
        }

        missing_components = {'cpu_usage', 'ram_usage', 'disk_usage'} - set(components_map.keys())
        if missing_components:
            logger.warning('Skipping VMware item invoice creation because plan components are missing. '
                           'Plan ID: %s. Missing components: %s', plan.id, ', '.join(missing_components))
            return

        cores_price = components_map['cpu_usage'] * source.cores
        ram_price = components_map['ram_usage'] * source.ram
        disk_price = components_map['disk_usage'] * source.total_disk
        total_price = cores_price + ram_price + disk_price

        """
        When resource configuration is switched, old invoice item
        is terminated and new invoice item is created.
        In order to avoid double counting we should ensure that
        there're no overlapping invoice items for the same scope.

        1) If old price is greater than new price,
           old invoice item end field should be adjusted to the end of current day
           and new invoice item start field should be adjusted to the start of next day.

        2) If old price is lower than new price,
           old invoice item end field should be adjusted to the end of previous day
           and new invoice item field should be adjusted to the start of current day.

        3) Finally, we need to cleanup planned invoice items when new item is created.
        """
        old_item = utils.get_vm_items().filter(
            invoice=invoice,
            end__day=start.day,
            object_id=source.pk,
        ).order_by('-unit_price').first()

        if old_item:
            if old_item.unit_price >= total_price:
                old_item.end = old_item.end.replace(hour=23, minute=59, second=59)
                old_item.save(update_fields=['end'])
                start = old_item.end + timedelta(seconds=1)

            else:
                start = old_item.end.replace(hour=0, minute=0, second=0)
                old_item.end = start - timedelta(seconds=1)
                old_item.save(update_fields=['end'])

        utils.get_vm_items().filter(
            invoice=invoice,
            start__day=start.day,
            object_id=source.pk,
        ).delete()

        details = self.get_details(source)
        service_provider_info = marketplace_utils.get_service_provider_info(resource)
        details.update(service_provider_info)
        invoices_models.GenericInvoiceItem.objects.create(
            scope=source,
            project=_get_project(source),
            unit_price=total_price,
            unit=plan.unit,
            product_code=plan.product_code,
            article_code=plan.article_code,
            invoice=invoice,
            start=start,
            end=end,
            details=details,
        )

    def get_name(self, source):
        return '{name} ({cores} CPU, {ram} MB RAM, {disk} MB disk)'.format(
            name=source.name,
            cores=source.cores,
            ram=source.ram,
            disk=source.total_disk,
        )
