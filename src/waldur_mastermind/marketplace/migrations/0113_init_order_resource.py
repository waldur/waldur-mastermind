from django.db import migrations


def init_order_resource(apps, schema_editor):
    Order = apps.get_model('marketplace', 'Order')
    Resource = apps.get_model('marketplace', 'Resource')
    for order in Order.objects.filter(resource__isnull=True):
        resource = Resource.objects.create(
            project=order.project,
            offering=order.offering,
            plan=order.plan,
            limits=order.limits,
            attributes=order.attributes,
            name=order.attributes.get('name') or 'resource',
        )
        order.resource = resource
        order.save(update_fields=['resource'])


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0112_delete_offeringpermission'),
    ]

    operations = [
        migrations.RunPython(init_order_resource),
    ]
