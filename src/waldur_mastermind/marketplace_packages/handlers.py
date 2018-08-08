from django.db import transaction

from . import utils


def create_offering_for_package_template(sender, instance, created=False, **kwargs):
    if created:
        utils.create_offering_for_package_template(instance)
    else:
        utils.update_offering_for_template(instance)


def sync_offering_attribute_with_template_component(sender, instance, created=False, **kwargs):
    transaction.on_commit(lambda: utils.sync_offering_attribute_with_template_component(instance))
