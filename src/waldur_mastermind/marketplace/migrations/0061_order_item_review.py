# Generated by Django 2.2.24 on 2021-10-15 09:26

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('marketplace', '0060_unique_offering_user'),
    ]

    operations = [
        migrations.AddField(
            model_name='orderitem',
            name='reviewed_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='orderitem',
            name='reviewed_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
