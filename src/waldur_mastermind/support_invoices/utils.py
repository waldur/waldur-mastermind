from django.contrib.contenttypes.models import ContentType
from django.db.models import Sum

from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.models import Resource
from waldur_mastermind.support import models as support_models


def get_quantity(plan_component, resource, start, end):
    offering_component = plan_component.component
    if offering_component.billing_type == marketplace_models.OfferingComponent.BillingTypes.FIXED:
        return plan_component.amount
    elif offering_component.billing_type == marketplace_models.OfferingComponent.BillingTypes.USAGE:
        return marketplace_models.ComponentUsage.objects.filter(
            component=offering_component,
            resource=resource,
            date__gte=start,
            date__lte=end).aggregate(amount=Sum('usage'))['amount'] or 0


def is_request_based(offering):
    return Resource.objects.filter(scope=offering).exists()


def get_offering_items():
    model_type = ContentType.objects.get_for_model(support_models.Offering)
    return invoices_models.GenericInvoiceItem.objects.filter(content_type=model_type)
