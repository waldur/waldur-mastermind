from django.db import migrations

from waldur_mastermind.marketplace_slurm import PLUGIN_NAME as SLURM_NAME


def set_default_disable_quotas(apps, schema_editor):
    Offering = apps.get_model('marketplace', 'Offering')
    for offering in Offering.objects.all():
        if offering.type == SLURM_NAME:
            for component in offering.components.all():
                component.disable_quotas = True
                component.save(update_fields=['disable_quotas'])


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0016_change_referrals_representation'),
    ]

    operations = [migrations.RunPython(set_default_disable_quotas)]
