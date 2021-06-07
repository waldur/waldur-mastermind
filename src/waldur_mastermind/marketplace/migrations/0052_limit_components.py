from django.db import migrations, models


def migrate_offering_components_to_limit(apps, schema_editor):
    OfferingComponent = apps.get_model('marketplace', 'OfferingComponent')
    OfferingComponent.objects.filter(offering__type='Packages.Template').update(
        billing_type='limit'
    )
    OfferingComponent.objects.filter(is_boolean=True, billing_type='usage').update(
        billing_type='limit'
    )


def cleanup_resource_limits(apps, schema_editor):
    Resource = apps.get_model('marketplace', 'Resource')
    for resource in Resource.objects.all():
        valid_types = set(
            resource.offering.components.filter(billing_type='limit').values_list(
                'type', flat=True
            )
        )
        current_types = set(resource.limits.keys())
        invalid_types = current_types - valid_types
        if invalid_types:
            print(f"Dropping Invalid types {invalid_types} in resource {resource.id}")
            resource.limits = {
                ctype: resource.limits[ctype] for ctype in current_types & valid_types
            }
            resource.save(update_fields=['limits'])


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0051_remove_offering_allowed_customers'),
    ]

    operations = [
        migrations.AlterField(
            model_name='offeringcomponent',
            name='billing_type',
            field=models.CharField(
                choices=[
                    ('fixed', 'Fixed-price'),
                    ('usage', 'Usage-based'),
                    ('limit', 'Limit-based'),
                    ('one', 'One-time'),
                    ('few', 'One-time on plan switch'),
                ],
                default='fixed',
                max_length=5,
            ),
        ),
        migrations.RunPython(migrate_offering_components_to_limit),
        migrations.RunPython(cleanup_resource_limits),
        migrations.RemoveField(model_name='offeringcomponent', name='disable_quotas',),
        migrations.RemoveField(
            model_name='offeringcomponent', name='use_limit_for_billing',
        ),
    ]
