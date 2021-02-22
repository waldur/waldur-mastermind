import datetime

from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations
from django.utils import timezone


def fill_component_usage(apps, schema_editor):
    AllocationUserUsage = apps.get_model('waldur_slurm', 'AllocationUserUsage')
    Allocation = apps.get_model('waldur_slurm', 'Allocation')
    ContentType = apps.get_model('contenttypes', 'ContentType')
    ComponentUsage = apps.get_model('marketplace', 'ComponentUsage')
    OfferingComponent = apps.get_model('marketplace', 'OfferingComponent')
    Resource = apps.get_model('marketplace', 'Resource')

    for item in AllocationUserUsage.objects.all():
        try:
            resource = Resource.objects.get(
                content_type=ContentType.objects.get_for_model(Allocation),
                object_id=item.allocation_usage.allocation_id,
            )
        except ObjectDoesNotExist:
            print(
                'Skipping allocation user usage synchronization because related marketplace resource is not found',
                item.allocation_usage.allocation_id,
            )
            continue

        for component_type in 'cpu', 'gpu', 'ram':
            try:
                offering_component = OfferingComponent.objects.get(
                    type=component_type, offering=resource.offering
                )
            except ObjectDoesNotExist:
                print(
                    'Skipping allocation user usage synchronization because related marketplace offering component is not found',
                    resource.offering_id,
                    component_type,
                )
                continue

            billing_period = timezone.make_aware(
                datetime.datetime(
                    year=item.allocation_usage.year,
                    month=item.allocation_usage.month,
                    day=1,
                )
            )

            if not ComponentUsage.objects.filter(
                resource=resource,
                component=offering_component,
                billing_period=billing_period,
            ).exists():
                ComponentUsage.objects.create(
                    resource=resource,
                    component=offering_component,
                    date=billing_period,
                    billing_period=billing_period,
                    usage=getattr(item, f'{component_type}_usage'),
                )


class Migration(migrations.Migration):

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('marketplace', '0023_category_i18n'),
        ('waldur_slurm', '0019_fill_allocation_user_usage'),
    ]

    operations = [
        migrations.RunPython(fill_component_usage),
    ]
