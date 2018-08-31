from waldur_mastermind.support.models import OfferingTemplate, Offering
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_mastermind.marketplace.models import OrderItem


def create_support_template(sender, instance, created=False, **kwargs):
    if instance.type != PLUGIN_NAME or not created:
        return

    template = OfferingTemplate.objects.create(
        name=instance.name,
        config=instance.options
    )
    instance.scope = template
    instance.save()


def change_order_item_state(sender, instance, created=False, **kwargs):
    if created:
        return

    if instance.tracker.has_changed('state'):
        try:
            order_item = OrderItem.objects.get(scope=instance)
        except OrderItem.DoesNotExist:
            return

        if instance.state == Offering.States.OK:
            order_item.set_state('done')

        if instance.state == Offering.States.TERMINATED:
            order_item.set_state('terminated')
