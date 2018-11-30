from django.db.models import Sum

from waldur_mastermind.marketplace import models as marketplace_models


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
