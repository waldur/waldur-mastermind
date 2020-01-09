from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.utils import get_resource_state


def create_marketplace_resource_for_imported_cluster(sender, instance, offering=None, plan=None, **kwargs):
    if not offering or not plan:
        # When cluster is imported directly (ie without marketplace),
        # marketplace resources are not created.
        return
    resource = marketplace_models.Resource(
        project=instance.service_project_link.project,
        state=get_resource_state(instance.state),
        name=instance.name,
        scope=instance,
        created=instance.created,
        plan=plan,
        offering=offering,
    )

    resource.init_cost()
    resource.save()
    resource.init_quotas()
