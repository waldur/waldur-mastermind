import datetime

from django.core.management.base import BaseCommand
from django.db import transaction

from waldur_core.structure import models as structure_models
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.registrators import RegistrationManager
from waldur_mastermind.marketplace import models as marketplace_models


class MoveResourceException(Exception):
    pass


@transaction.atomic
def move_resource(resource, project):
    old_project = resource.project

    linked_offerings = marketplace_models.Offering.objects.filter(
        scope=resource.scope, allowed_customers__in=[old_project.customer],
    )

    offering: marketplace_models.Offering
    for offering in linked_offerings:
        offering.allowed_customers.remove(old_project.customer)
        offering.allowed_customers.add(project.customer)

    resource.project = project
    resource.save(update_fields=['project'])

    spl, _ = resource.scope.service_project_link._meta.model.objects.get_or_create(
        service=resource.scope.service_project_link.service, project=project,
    )

    resource.scope.service_project_link = spl
    resource.scope.save(update_fields=['service_project_link'])

    order_ids = resource.orderitem_set.values_list('order_id', flat=True)
    for order in marketplace_models.Order.objects.filter(pk__in=order_ids):

        if order.items.exclude(resource=resource).exists():
            raise MoveResourceException(
                'Resource moving is not possible, '
                'because related orders are related to other resources.'
            )

        order.project = project
        order.save(update_fields=['project'])

    for invoice_item in invoices_models.InvoiceItem.objects.filter(
        scope=resource,
        invoice__state=invoices_models.Invoice.States.PENDING,
        project=old_project,
    ):

        start_invoice = invoice_item.invoice

        target_invoice, _ = RegistrationManager.get_or_create_invoice(
            project.customer,
            date=datetime.date(
                year=start_invoice.year, month=start_invoice.month, day=1
            ),
        )

        if target_invoice.state != invoices_models.Invoice.States.PENDING:
            raise MoveResourceException(
                'Resource moving is not possible, '
                'because invoice items moving is not possible.'
            )

        invoice_item.project = project
        invoice_item.project_uuid = project.uuid.hex
        invoice_item.project_name = project.name
        invoice_item.invoice = target_invoice
        invoice_item.save(
            update_fields=['project', 'project_uuid', 'project_name', 'invoice']
        )

        start_invoice.update_current_cost()
        target_invoice.update_current_cost()


class Command(BaseCommand):
    help = "Move a marketplace resource to a different project."

    def add_arguments(self, parser):
        parser.add_argument(
            '-p',
            '--project',
            dest='project_uuid',
            required=True,
            help='Target project UUID',
        )
        parser.add_argument(
            '-r',
            '--resource',
            dest='resource_uuid',
            required=True,
            help='UUID of a marketplace resource to move.',
        )

    def handle(self, project_uuid, resource_uuid, *args, **options):
        try:
            project = structure_models.Project.objects.get(uuid=project_uuid)
        except structure_models.Project.DoesNotExist:
            self.stdout.write(self.style.ERROR('Project is not found.'))
            return
        except ValueError:
            self.stdout.write(self.style.ERROR('Project UUID is not valid.'))
            return

        try:
            resource = marketplace_models.Resource.objects.get(uuid=resource_uuid)
        except marketplace_models.Resource.DoesNotExist:
            self.stdout.write(self.style.ERROR('Resource is not found.'))
            return
        except ValueError:
            self.stdout.write(self.style.ERROR('Resource UUID is not valid.'))
            return

        try:
            move_resource(resource, project)
            self.stdout.write(
                self.style.SUCCESS('Resource has been moved to another project.')
            )
        except MoveResourceException as e:
            self.stdout.write(self.style.ERROR(e))
