from waldur_mastermind.support.models import OfferingTemplate
from waldur_mastermind.marketplace_support import PLUGIN_NAME


def create_support_template(sender, instance, created=False, **kwargs):
    if instance.type != PLUGIN_NAME or not created:
        return

    template = OfferingTemplate.objects.create(
        name=instance.name,
        config=instance.options
    )
    instance.scope = template
    instance.save()
