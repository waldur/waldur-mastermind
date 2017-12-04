# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
from decimal import Decimal
import waldur_core.core.fields
import django.core.validators
import waldur_core.core.validators


class Migration(migrations.Migration):

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='PackageComponent',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('type', models.CharField(max_length=50, choices=[('ram', 'RAM, MB'), ('cores', 'Cores'), ('storage', 'Storage, MB')])),
                ('amount', models.PositiveIntegerField(default=0)),
                ('price', models.DecimalField(default=0, help_text='The price per unit of amount', verbose_name='Price per hour', max_digits=13, decimal_places=7, validators=[django.core.validators.MinValueValidator(Decimal('0'))])),
            ],
        ),
        migrations.CreateModel(
            name='PackageTemplate',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('icon_url', models.URLField(verbose_name='icon url', blank=True)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('type', models.CharField(default='openstack', max_length=50, choices=[('openstack', 'OpenStack')])),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.AddField(
            model_name='packagecomponent',
            name='template',
            field=models.ForeignKey(related_name='components', to='packages.PackageTemplate'),
        ),
        migrations.AlterUniqueTogether(
            name='packagecomponent',
            unique_together=set([('type', 'template')]),
        ),
    ]
