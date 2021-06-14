from django.db import migrations


def restore_limits(apps, schema_editor):
    Resource = apps.get_model('marketplace', 'Resource')
    OrderItem = apps.get_model('marketplace', 'OrderItem')

    for resource in Resource.objects.filter(limits={}):
        order_item = (
            OrderItem.objects.filter(resource=resource)
            .exclude(limits={})
            .order_by('created', 'modified')
            .last()
        )
        if not order_item:
            continue
        valid_types = set(
            resource.offering.components.filter(billing_type='limit').values_list(
                'type', flat=True
            )
        )
        current_types = set(order_item.limits.keys())
        resource.limits = {
            ctype: order_item.limits[ctype] for ctype in current_types & valid_types
        }
        resource.save(update_fields=['limits'])


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0053_resource_end_date'),
    ]

    operations = [
        migrations.RunPython(restore_limits),
    ]
