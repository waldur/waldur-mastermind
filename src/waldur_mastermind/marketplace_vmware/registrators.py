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
        details = self.get_details(resource)
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

    def get_details(self, source):
        name = '{name} ({cores} CPU, {ram} MB RAM, {disk} MB disk)'.format(
            name=source.name,
            cores=source.cores,
            ram=source.ram,
            disk=source.total_disk,
        )
        details = {'name': name}
        service_provider_info = marketplace_utils.get_service_provider_info(source)
        details.update(service_provider_info)
        return details

    def terminate(self, source, now=None):
        super(VirtualMachineRegistrator, self).terminate(source, now)
        utils.get_vm_items().filter(object_id=source.id).update(object_id=None)
