from django.db import migrations


def pull_settings(apps, schema_editor):
    Cluster = apps.get_model('waldur_rancher', 'Cluster')
    Catalog = apps.get_model('waldur_rancher', 'Catalog')
    Project = apps.get_model('waldur_rancher', 'Project')
    Namespace = apps.get_model('waldur_rancher', 'Namespace')

    ServiceSettings = apps.get_model('structure', 'ServiceSettings')
    ContentType = apps.get_model('contenttypes', 'ContentType')
    settings_content_type = ContentType.objects.get_for_model(ServiceSettings)
    cluster_content_type = ContentType.objects.get_for_model(Cluster)
    project_content_type = ContentType.objects.get_for_model(Project)

    for cluster in Cluster.objects.all():
        cluster.settings = cluster.service_project_link.service.settings
        cluster.save()

    for project in Project.objects.all():
        if project.cluster:
            project.settings = project.cluster.service_project_link.service.settings
            project.save()

    for namespace in Namespace.objects.all():
        if namespace.project:
            namespace.settings = (
                namespace.project.cluster.service_project_link.service.settings
            )
            namespace.save()

    for catalog in Catalog.objects.all():
        if catalog.content_type == settings_content_type:
            catalog.settings_id = catalog.object_id
        elif catalog.content_type == cluster_content_type:
            try:
                scope = Cluster.objects.get(id=catalog.object_id)
                catalog.settings = scope.service_project_link.service.settings
            except Cluster.DoesNotExist:
                pass
        elif catalog.content_type == project_content_type:
            try:
                scope = Project.objects.get(id=catalog.object_id)
                catalog.settings = scope.cluster.service_project_link.service.settings
            except Project.DoesNotExist:
                pass
        catalog.save()


class Migration(migrations.Migration):

    dependencies = [
        ('waldur_rancher', '0017_add_settings_and_template'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [migrations.RunPython(pull_settings)]
