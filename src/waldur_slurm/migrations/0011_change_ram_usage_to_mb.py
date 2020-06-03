from django.db import migrations


def update_ram_usage(apps, schema_editor):
    bytes_in_mb = 2 ** 20
    app_label = 'waldur_slurm'

    Allocation = apps.get_model(app_label, 'Allocation')
    for allocation in Allocation.objects.all():
        if allocation.ram_usage != 0:
            allocation.ram_usage = allocation.ram_usage // bytes_in_mb
            allocation.save(update_fields=['ram_usage'])

    AllocationUsage = apps.get_model(app_label, 'AllocationUsage')
    for allocation_usage in AllocationUsage.objects.all():
        if allocation_usage.ram_usage != 0:
            allocation_usage.ram_usage = allocation_usage.ram_usage // bytes_in_mb
            allocation_usage.save(update_fields=['ram_usage'])

    AllocationUserUsage = apps.get_model(app_label, 'AllocationUserUsage')
    for allocation_user_usage in AllocationUserUsage.objects.all():
        if allocation_user_usage.ram_usage != 0:
            allocation_user_usage.ram_usage = (
                allocation_user_usage.ram_usage // bytes_in_mb
            )
            allocation_user_usage.save(update_fields=['ram_usage'])


class Migration(migrations.Migration):

    dependencies = [
        ('waldur_slurm', '0010_change_default_ram_limit'),
    ]

    operations = [migrations.RunPython(update_ram_usage)]
