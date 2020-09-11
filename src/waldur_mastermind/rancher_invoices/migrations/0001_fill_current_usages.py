from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations


def fill_current_usages(apps, schema_editor):
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Resource = apps.get_model('marketplace', 'Resource')
    Cluster = apps.get_model('waldur_rancher', 'Cluster')
    model_type = ContentType.objects.get_for_model(Cluster)
    STATE_OK = 3

    for resource in Resource.objects.filter(content_type=model_type):
        try:
            cluster = Cluster.objects.get(id=resource.object_id)
        except ObjectDoesNotExist:
            pass
        else:
            usage = cluster.node_set.filter(state=STATE_OK).count()
            resource.current_usages = {'nodes': usage}
            resource.save(update_fields=['current_usages'])


class Migration(migrations.Migration):

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('marketplace', '0022_extend_description_limits'),
        ('waldur_rancher', '0032_service'),
    ]

    operations = [
        migrations.RunPython(fill_current_usages),
    ]
