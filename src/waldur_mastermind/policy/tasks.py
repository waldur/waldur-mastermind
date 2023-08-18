from celery import shared_task

from waldur_core.core import utils as core_utils
from waldur_core.structure import models as structure_models


@shared_task(name='waldur_mastermind.policy.notify_about_limit_cost')
def notify_about_limit_cost(serialized_scope, serialized_policy):
    scope = core_utils.deserialize_instance(serialized_scope)
    policy = core_utils.deserialize_instance(serialized_policy)
    role = (
        structure_models.CustomerRole.OWNER
        if isinstance(scope, structure_models.Customer)
        else None
    )
    emails = scope.get_user_mails(role)

    if emails:
        context = {
            'project_name': policy.project.name,
            'project_url': core_utils.format_homeport_link(
                'projects/{project_uuid}/', project_uuid=policy.project.uuid.hex
            ),
            'limit': policy.limit_cost,
        }
        core_utils.broadcast_mail(
            'marketplace_policy',
            'notification_about_project_cost_exceeded_limit',
            context,
            emails,
        )
