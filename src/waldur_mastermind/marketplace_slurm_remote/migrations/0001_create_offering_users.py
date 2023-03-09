from django.db import migrations


class States:
    DRAFT = 1
    ACTIVE = 2
    PAUSED = 3
    ARCHIVED = 4


def create_offering_users_for_existing_users(apps, schema_editor):
    from waldur_mastermind.marketplace_slurm_remote import utils

    Offering = apps.get_model('marketplace', 'Offering')
    offerings = Offering.objects.filter(
        type='Marketplace.Slurm',
        state__in=[States.ACTIVE, States.PAUSED],
    )

    for offering in offerings:
        if 'username_generation_policy' not in offering.plugin_options:
            offering.plugin_options.update({'username_generation_policy': 'freeipa'})
            offering.save(update_fields=['plugin_options'])

    utils.user_offerings_mapping(offerings, create=True)


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0082_offeringuser_propagation_date'),
    ]

    operations = [
        migrations.RunPython(create_offering_users_for_existing_users),
    ]
