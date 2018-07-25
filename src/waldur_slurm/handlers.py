from django.db import transaction
from django.db.models import Sum

from waldur_core.core import utils as core_utils
from waldur_freeipa import models as freeipa_models

from . import models, tasks, utils


def process_user_creation(sender, instance, created=False, **kwargs):
    if not created:
        return
    transaction.on_commit(lambda:
                          tasks.add_user.delay(core_utils.serialize_instance(instance)))


def process_user_deletion(sender, instance, **kwargs):
    transaction.on_commit(lambda:
                          tasks.delete_user.delay(core_utils.serialize_instance(instance)))


def process_role_granted(sender, structure, user, role, **kwargs):
    try:
        freeipa_profile = freeipa_models.Profile.objects.get(user=user)
        serialized_profile = core_utils.serialize_instance(freeipa_profile)
        serialized_structure = core_utils.serialize_instance(structure)
        transaction.on_commit(lambda:
                              tasks.process_role_granted.delay(serialized_profile, serialized_structure))
    except freeipa_models.Profile.DoesNotExist:
        pass


def process_role_revoked(sender, structure, user, role, **kwargs):
    try:
        freeipa_profile = freeipa_models.Profile.objects.get(user=user)
        serialized_profile = core_utils.serialize_instance(freeipa_profile)
        serialized_structure = core_utils.serialize_instance(structure)
        transaction.on_commit(lambda:
                              tasks.process_role_revoked.delay(serialized_profile, serialized_structure))
    except freeipa_models.Profile.DoesNotExist:
        pass


def update_quotas_on_allocation_usage_update(sender, instance, created=False, **kwargs):
    if created:
        return

    allocation = instance
    if not allocation.usage_changed():
        return

    project = allocation.service_project_link.project
    update_quotas(project, models.Allocation.Permissions.project_path)
    update_quotas(project.customer, models.Allocation.Permissions.customer_path)


def update_quotas(scope, path):
    qs = models.Allocation.objects.filter(**{path: scope}).values(path)
    for quota in utils.FIELD_NAMES:
        qs = qs.annotate(**{'total_%s' % quota: Sum(quota)})
    qs = list(qs)[0]

    for quota in utils.FIELD_NAMES:
        scope.set_quota_usage(utils.MAPPING[quota], qs['total_%s' % quota])
