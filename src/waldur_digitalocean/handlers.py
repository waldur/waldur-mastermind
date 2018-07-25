from waldur_core.core import models as core_models, tasks as core_tasks, utils as core_utils
from waldur_core.structure import models as structure_models, filters as structure_filters

from . import models


def remove_ssh_keys_from_service(sender, structure, user, role, **kwargs):
    """ Remove user ssh keys if he doesn't have access to service anymore. """
    if isinstance(structure, structure_models.Project):
        lost_services = models.DigitalOceanService.objects.filter(customer__projects=structure)
    elif isinstance(structure, structure_models.Customer):
        lost_services = models.DigitalOceanService.objects.filter(customer=structure)
    else:
        return
    lost_settings = [service.settings for service in lost_services]
    visible_services = structure_filters.filter_queryset_for_user(models.DigitalOceanService.objects.all(), user)
    visible_settings = [service.settings for service in visible_services]
    settings_list = set([settings for settings in lost_settings if settings not in visible_settings])

    ssh_keys = core_models.SshPublicKey.objects.filter(user=user)
    for settings in settings_list:
        serialized_settings = core_utils.serialize_instance(settings)
        for ssh_key in ssh_keys:
            core_tasks.IndependentBackendMethodTask().delay(
                serialized_settings, 'remove_ssh_key', ssh_key.name, ssh_key.fingerprint)


def remove_ssh_key_from_service_settings_on_deletion(sender, instance, **kwargs):
    ssh_key = instance
    user = ssh_key.user
    services = structure_filters.filter_queryset_for_user(models.DigitalOceanService.objects.all(), user)
    settings_list = structure_models.ServiceSettings.objects.filter(digitaloceanservice=services)
    for settings in settings_list:
        serialized_settings = core_utils.serialize_instance(settings)
        core_tasks.IndependentBackendMethodTask().delay(
            serialized_settings, 'remove_ssh_key', ssh_key.name, ssh_key.fingerprint)
