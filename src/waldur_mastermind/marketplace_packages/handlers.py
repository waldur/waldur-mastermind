from . import utils


def create_offering_and_plan_for_package_template(sender, instance, created=False, **kwargs):
    if created:
        utils.create_offering_and_plan_for_package_template(instance)
    else:
        utils.update_plan_for_template(instance)


def update_offering_for_service_settings(sender, instance, created=False, **kwargs):
    if not created:
        utils.update_offering_for_service_settings(instance)
