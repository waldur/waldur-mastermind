import django.db.models.deletion
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.core.validators


def fill_offering_endpoint(apps, schema_editor):
    Offering = apps.get_model('marketplace', 'Offering')
    OfferingAccessEndpoint = apps.get_model('marketplace', 'OfferingAccessEndpoint')
    for offering in Offering.objects.exclude(access_url='').exclude(
        access_url__isnull=True
    ):
        OfferingAccessEndpoint.objects.create(
            offering=offering, name='Access URL', url=offering.access_url
        )


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0092_resource_requested_downscaling'),
    ]

    operations = [
        migrations.CreateModel(
            name='OfferingAccessEndpoint',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('url', waldur_core.core.fields.BackendURLField()),
                (
                    'offering',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='endpoints',
                        to='marketplace.offering',
                    ),
                ),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='ResourceAccessEndpoint',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('url', waldur_core.core.fields.BackendURLField()),
                (
                    'resource',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='endpoints',
                        to='marketplace.resource',
                    ),
                ),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.RunPython(fill_offering_endpoint),
        migrations.RemoveField(
            model_name='offering',
            name='access_url',
        ),
    ]
