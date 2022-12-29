import logging

from django.db.models import Q

from waldur_mastermind.promotions import models

logger = logging.getLogger(__name__)


def create_discounted_resource(sender, instance, created=False, **kwargs):
    if created:
        return

    if not instance.tracker.has_changed('state'):
        return

    if instance.state != instance.States.OK:
        return

    resource = instance
    order_item = resource.orderitem_set.first()

    if not order_item:
        return

    coupon = order_item.attributes.get('coupon', '')

    for campaign in models.Campaign.objects.filter(
        state=models.Campaign.States.ACTIVE,
        start_date__lte=resource.created,
        end_date__gte=resource.created,
    ).filter(Q(coupon='') | Q(coupon=coupon)):
        if campaign.check_resource_on_conditions_of_campaign(resource):
            models.DiscountedResource.objects.get_or_create(
                campaign=campaign,
                resource=resource,
            )


def apply_campaign_to_pending_invoices(sender, instance, created=False, **kwargs):
    from waldur_mastermind.invoices import models as invoices_models
    from waldur_mastermind.marketplace import models as marketplace_models

    campaign = instance

    if created:
        return

    if not campaign.tracker.has_changed('state'):
        return

    if campaign.state != models.Campaign.States.ACTIVE:
        return

    if not campaign.auto_apply:
        return

    #  check if there are resources that match the campaign.
    #  If there are, then we create object of DiscountedResource
    for resource in marketplace_models.Resource.objects.filter(
        state=marketplace_models.Resource.States.OK,
    ):

        if campaign.check_resource_on_conditions_of_campaign(resource):
            models.DiscountedResource.objects.create(
                campaign=campaign,
                resource=resource,
            )

    # We discount the price if the campaign has already started,
    # otherwise we only create an object of the DiscountedResource model,
    # and the discount will be created in the MarketplaceRegistrator
    # when the invoice is created.
    for invoice_item in invoices_models.InvoiceItem.objects.filter(
        invoice__state=invoices_models.Invoice.States.PENDING,
        invoice__year=campaign.start_date.year,
        invoice__month=campaign.start_date.month,
    ):
        resource = invoice_item.resource

        if campaign.check_resource_on_conditions_of_campaign(resource):
            unit_price = invoice_item.details.get('unit_price', invoice_item.unit_price)
            discount_price = campaign.get_discount_price(unit_price)

            if discount_price < invoice_item.unit_price:
                if (
                    invoice_item.invoice.year == campaign.start_date.year
                    and invoice_item.invoice.month == campaign.start_date.month
                ):
                    invoice_item.unit_price = discount_price
                    invoice_item.details['campaign_uuid'] = campaign.uuid.hex
                    invoice_item.details['unit_price'] = float(unit_price)
                    invoice_item.save()
