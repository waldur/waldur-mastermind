from django.db import migrations


def fill_allocation_user_usage(apps, schema_editor):
    AllocationUserUsage = apps.get_model('waldur_slurm', 'AllocationUserUsage')

    for item in AllocationUserUsage.objects.all():
        item.allocation = item.allocation_usage.allocation
        item.year = item.allocation_usage.year
        item.month = item.allocation_usage.month
        item.save(update_fields=['allocation', 'year', 'month'])


class Migration(migrations.Migration):

    dependencies = [
        ('waldur_slurm', '0018_add_allocation_month_year'),
    ]

    operations = [
        migrations.RunPython(fill_allocation_user_usage),
    ]
