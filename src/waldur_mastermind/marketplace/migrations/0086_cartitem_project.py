# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


def remove_all_cart_items(apps, schema_editor):
    CartItem = apps.get_model('marketplace', 'CartItem')
    CartItem.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0009_project_is_removed'),
        ('marketplace', '0085_terms_of_service_field'),
    ]

    operations = [
        migrations.RunPython(remove_all_cart_items),
        migrations.AddField(
            model_name='cartitem',
            name='project',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='structure.Project'),
            preserve_default=False,
        ),
    ]
