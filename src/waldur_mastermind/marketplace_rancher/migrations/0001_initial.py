from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations


def create_offering_users_for_rancher_users(apps, schema_editor):
    Offering = apps.get_model('marketplace', 'Offering')
    OfferingUser = apps.get_model('marketplace', 'OfferingUser')
    RancherUser = apps.get_model('waldur_rancher', 'RancherUser')
    ServiceSettings = apps.get_model('structure', 'ServiceSettings')
    ContentType = apps.get_model('contenttypes', 'ContentType')
    settings_content_type = ContentType.objects.get_for_model(ServiceSettings)

    for rancher_user in RancherUser.objects.all():
        try:
            offering = Offering.objects.get(
                content_type=settings_content_type, object_id=rancher_user.settings_id
            )
        except ObjectDoesNotExist:
            print(
                'Skipping Rancher user synchronization because offering is not found. '
                'Rancher settings ID: %s',
                rancher_user.settings.id,
            )
            continue

        OfferingUser.objects.create(
            offering=offering,
            user=rancher_user.user,
            username=rancher_user.user.username,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0051_remove_offering_allowed_customers'),
        ('waldur_rancher', '0036_cluster_management_security_group'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [migrations.RunPython(create_offering_users_for_rancher_users)]
