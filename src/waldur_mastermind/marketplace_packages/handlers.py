from waldur_mastermind.packages import models as package_models
from waldur_mastermind.marketplace import models as marketplace_models

from . import utils


def create_offering_and_plan_for_package_template(sender, instance, created=False, **kwargs):
    if created:
        utils.create_offering_and_plan_for_package_template(instance)
    else:
        utils.update_plan_for_template(instance)


def update_offering_for_service_settings(sender, instance, created=False, **kwargs):
    if not created:
        utils.update_offering_for_service_settings(instance)


def change_order_item_state(sender, instance, created=False, **kwargs):
    if created:
        return

    if not instance.tracker.has_changed('state'):
        return

    if instance.tracker.previous('state') != instance.States.CREATION_SCHEDULED:
        return

    if instance.state in [instance.States.OK, instance.States.ERRED]:
        try:
            openstack_package = package_models.OpenStackPackage.objects.get(tenant=instance)
            order_item = marketplace_models.OrderItem.objects.get(scope=openstack_package)
        except package_models.OpenStackPackage.DoesNotExist:
            return
        except marketplace_models.OrderItem.DoesNotExist:
            return

        if instance.state == instance.States.OK:
            order_item.set_state('done')

        if instance.state == instance.States.ERRED:
            order_item.set_state('erred')
