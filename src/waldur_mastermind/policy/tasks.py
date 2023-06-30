from celery import shared_task

from waldur_core.core import utils as core_utils


@shared_task(name='waldur_mastermind.policy.notify_about_limit_cost')
def notify_about_limit_cost(serialized_scope, serialized_policy):
    scope = core_utils.deserialize_instance(serialized_scope)
    policy = core_utils.deserialize_instance(serialized_policy)
    emails = scope.get_user_mails()

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
