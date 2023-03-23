from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations


def link_parent_resource(apps, schema_editor):
    """
    Consider for example the following chain of dependencies:
    Marketplace resource for OpenStack instance refers to
    Marketplace offering for OpenStack instance which refers to
    Service settings which refers to OpenStack tenant.
    """
    Resource = apps.get_model('marketplace', 'Resource')
    ContentType = apps.get_model('contenttypes', 'ContentType')
    ServiceSettings = apps.get_model('structure', 'ServiceSettings')

    for resource in Resource.objects.all():
        offering = resource.offering
        if not offering.content_type_id or not offering.object_id:
            continue

        ct = ContentType.objects.get_for_id(offering.content_type_id)
        try:
            model_class = apps.get_model(ct.app_label, ct.model)
        except LookupError:
            continue

        if model_class != ServiceSettings:
            continue

        try:
            offering_scope = model_class.objects.get(id=offering.object_id)
        except ObjectDoesNotExist:
            print(
                f'Unable to get object with object ID: {offering.object_id}, '
                f'content type: {ct.app_label}, {ct.model}'
            )
            continue

        if not offering_scope.content_type_id or not offering_scope.object_id:
            continue

        try:
            parent_resource = Resource.objects.get(
                content_type_id=offering_scope.content_type_id,
                object_id=offering_scope.object_id,
            )
        except ObjectDoesNotExist:
            print(
                f'Unable to get resource by scope with object ID: {offering_scope.object_id}, '
                f'content type: {ct.app_label}, {ct.model}'
            )
            continue
        else:
            resource.parent = parent_resource
            resource.save()


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0001_squashed_0076'),
    ]

    operations = [
        migrations.RunPython(link_parent_resource),
    ]
