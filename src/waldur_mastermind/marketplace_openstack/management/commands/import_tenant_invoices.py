from django.contrib.contenttypes.models import ContentType

from waldur_core.core.utils import DryRunCommand
from waldur_mastermind.invoices.models import InvoiceItem
from waldur_mastermind.marketplace.models import Resource
from waldur_mastermind.packages.models import OpenStackPackage


class Command(DryRunCommand):
    help = """Import OpenStack invoice items from packages application."""

    def handle(self, dry_run, *args, **options):
        ct = ContentType.objects.get_for_model(OpenStackPackage)
        for invoice_item in InvoiceItem.objects.filter(content_type=ct).exclude(object_id=None):
            package = invoice_item.scope
            if not package:
                continue
            tenant = package.tenant
            try:
                resource = Resource.objects.get(scope=tenant)
            except Resource.DoesNotExist:
                self.stdout.write(self.style.ERROR('Marketplace resource for tenant with ID %s is not found.') %
                                  invoice_item.scope.pk)
            else:
                if dry_run:
                    self.stdout.write(self.style.SUCCESS('Importing invoice item for package with ID %s.') %
                                      invoice_item.scope.pk)
                else:
                    invoice_item.scope = resource
                    invoice_item.save()
