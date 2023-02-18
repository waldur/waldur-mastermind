# Generated by Django 2.2.24 on 2021-11-04 11:58

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('invoices', '0057_long_project_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoice',
            name='payment_url',
            field=models.URLField(
                blank=True, help_text='URL for initiating payment via payment gateway.'
            ),
        ),
        migrations.AddField(
            model_name='invoice',
            name='reference_number',
            field=models.CharField(
                blank=True,
                help_text='Reference number associated with the invoice.',
                max_length=300,
            ),
        ),
    ]
