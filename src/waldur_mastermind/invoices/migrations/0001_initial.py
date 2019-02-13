# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import waldur_core.core.fields
from decimal import Decimal
import django.db.models.deletion
import django.core.validators
import waldur_mastermind.invoices.utils


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0037_remove_customer_billing_backend_id'),
        ('packages', '0002_openstack_packages'),
    ]

    operations = [
        migrations.CreateModel(
            name='Invoice',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('month', models.PositiveSmallIntegerField(default=waldur_mastermind.invoices.utils.get_current_month, validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(12)])),
                ('year', models.PositiveSmallIntegerField(default=waldur_mastermind.invoices.utils.get_current_year)),
                ('state', models.CharField(default='pending', max_length=7, choices=[('billed', 'Billed'), ('paid', 'Paid'), ('pending', 'Pending')])),
                ('customer', models.ForeignKey(verbose_name='organization', related_name='+', to='structure.Customer')),
            ],
        ),
        migrations.CreateModel(
            name='OpenStackItem',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('package_details', waldur_core.core.fields.JSONField(default={}, help_text='Stores data about package', blank=True)),
                ('price', models.DecimalField(help_text='Price is calculated on a monthly basis.', max_digits=13, decimal_places=7, validators=[django.core.validators.MinValueValidator(Decimal('0'))])),
                ('start', models.DateTimeField(default=waldur_mastermind.invoices.utils.get_current_month_start, help_text='Date and time when item usage has started.')),
                ('end', models.DateTimeField(default=waldur_mastermind.invoices.utils.get_current_month_end, help_text='Date and time when item usage has ended.')),
                ('invoice', models.ForeignKey(related_name='+', to='invoices.Invoice')),
                ('package', models.ForeignKey(related_name='+', on_delete=django.db.models.deletion.SET_NULL, to='packages.OpenStackPackage', null=True)),
            ],
        ),
        migrations.AlterUniqueTogether(
            name='invoice',
            unique_together=set([('customer', 'month', 'year')]),
        ),
    ]
