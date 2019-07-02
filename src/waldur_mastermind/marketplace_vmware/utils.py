from django.contrib.contenttypes.models import ContentType
from waldur_mastermind.invoices import models as invoice_models
from waldur_vmware import models as vmware_models


def get_vm_items():
    model_type = ContentType.objects.get_for_model(vmware_models.VirtualMachine)
    return invoice_models.GenericInvoiceItem.objects.filter(content_type=model_type)
