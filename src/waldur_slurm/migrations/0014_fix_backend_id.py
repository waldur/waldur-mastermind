from django.db import migrations


def update_backend_id(apps, schema_editor):
    app_label = 'waldur_slurm'

    Allocation = apps.get_model(app_label, 'Allocation')

    for allocation in Allocation.objects.all():
        allocation.backend_id = allocation.backend_id.lower()
        allocation.save(update_fields=['backend_id'])


class Migration(migrations.Migration):

    dependencies = [
        ('waldur_slurm', '0013_extend_description_limits'),
    ]

    operations = [migrations.RunPython(update_backend_id)]
