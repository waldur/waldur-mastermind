from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import model_utils.fields
import waldur_core.core.fields


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('structure', '0052_customer_subnets'),
    ]

    operations = [
        migrations.CreateModel(
            name='ExpertProvider',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('customer', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='structure.Customer')),
            ],
            options={
                'verbose_name': 'Expert providers',
            },
        ),
    ]
