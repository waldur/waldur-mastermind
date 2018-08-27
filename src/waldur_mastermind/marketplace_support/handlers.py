from waldur_mastermind.support.models import OfferingTemplate
from waldur_mastermind.marketplace_support import PLUGIN_NAME


def create_support_template(sender, instance, created=False, **kwargs):
    if instance.type == PLUGIN_NAME:
        if created:
            OfferingTemplate.objects.create(
                name=instance.name,
                config=instance.options
            )
        else:
            # Because Marketplace offering is not editable now.
            pass
