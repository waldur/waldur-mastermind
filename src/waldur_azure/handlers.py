
def copy_cloud_service_name_on_service_creation(sender, instance, created=False, **kwargs):
    if not created:
        return

    service_project_link = instance
    if instance.cloud_service_name or 'cloud_service_name' not in service_project_link.service.settings.options:
        return

    cloud_service_name = service_project_link.service.settings.options['cloud_service_name']
    instance.cloud_service_name = cloud_service_name
    instance.save()
