from rest_framework import exceptions

from waldur_core.core import utils as core_utils
from waldur_mastermind.policy import tasks


def notify_project_team(policy):
    serialized_scope = core_utils.serialize_instance(policy.project)
    serialized_policy = core_utils.serialize_instance(policy)
    tasks.notify_about_limit_cost.delay(serialized_scope, serialized_policy)


notify_project_team.one_time_action = True


def notify_organization_owners(policy):
    serialized_scope = core_utils.serialize_instance(policy.project.customer)
    serialized_policy = core_utils.serialize_instance(policy)
    tasks.notify_about_limit_cost.delay(serialized_scope, serialized_policy)


notify_organization_owners.one_time_action = True


def block_creation_of_new_resources(policy, created):
    if created:
        raise exceptions.ValidationError(
            'Creation of new resources in this project is not available due to a policy.'
        )


block_creation_of_new_resources.one_time_action = False


def block_modification_of_existing_resources(policy, created):
    if not created:
        raise exceptions.ValidationError(
            'Modification of new resources in this project is not available due to a policy.'
        )


block_modification_of_existing_resources.one_time_action = False
