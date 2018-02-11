# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import model_utils.fields
import waldur_core.core.fields
import django.utils.timezone
from decimal import Decimal
import django.db.models.deletion
import django.core.validators
import waldur_core.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0039_remove_permission_groups'),
        ('support', '0005_issue_first_response_sla'),
    ]

    operations = [
        migrations.CreateModel(
            name='Offering',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('type', models.CharField(max_length=255, blank=True)),
                ('type_label', models.CharField(max_length=255, blank=True)),
                ('price', models.DecimalField(decimal_places=7, default=0, max_digits=13, validators=[django.core.validators.MinValueValidator(Decimal('0'))], help_text='The price per unit of offering', verbose_name='Price per day')),
                ('state', models.CharField(default='requested', max_length=30, choices=[('requested', 'Requested'), ('ok', 'OK'), ('terminated', 'Terminated')])),
                ('issue', models.ForeignKey(on_delete=django.db.models.deletion.SET_NULL, to='support.Issue', null=True)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='structure.Project', null=True)),
            ],
            options={
                'abstract': False,
                'ordering': ['-created'],
                'verbose_name': 'Request',
                'verbose_name_plural': 'Requests',
            },
        ),
    ]
