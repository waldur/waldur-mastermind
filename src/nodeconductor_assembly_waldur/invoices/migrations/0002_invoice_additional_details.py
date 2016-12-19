# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0037_remove_customer_billing_backend_id'),
        ('invoices', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='PaymentDetails',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('company', models.CharField(max_length=150, blank=True)),
                ('address', models.CharField(max_length=300, blank=True)),
                ('country', models.CharField(max_length=50, blank=True)),
                ('email', models.EmailField(max_length=75, blank=True)),
                ('postal', models.CharField(max_length=20, blank=True)),
                ('phone', models.CharField(max_length=20, blank=True)),
                ('bank', models.CharField(max_length=150, blank=True)),
                ('account', models.CharField(max_length=50, blank=True)),
                ('customer', models.OneToOneField(related_name='payment_details', to='structure.Customer')),
            ],
            options={'verbose_name': 'Payment details', 'verbose_name_plural': 'Payment details'},
        ),
        migrations.AddField(
            model_name='invoice',
            name='invoice_date',
            field=models.DateField(help_text='Date then invoice moved from state pending to created.', null=True, blank=True),
        ),
        migrations.AddField(
            model_name='invoice',
            name='tax_percent',
            field=models.DecimalField(default=0, max_digits=4, decimal_places=2, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)]),
        ),
        migrations.AlterField(
            model_name='invoice',
            name='state',
            field=models.CharField(default='pending', max_length=30, choices=[('pending', 'Pending'), ('created', 'Created'), ('paid', 'Paid'), ('canceled', 'Canceled')]),
        ),
    ]
