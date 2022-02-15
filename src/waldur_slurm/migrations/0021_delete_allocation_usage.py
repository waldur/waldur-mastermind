from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('waldur_slurm', '0020_fill_component_usage'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='allocationuserusage',
            name='allocation_usage',
        ),
        migrations.DeleteModel(
            name='AllocationUsage',
        ),
    ]
