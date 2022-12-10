# Generated by Django 3.2.16 on 2022-12-07 11:06

from django.db import migrations

MARKER = '999999999'


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0037_alter_customer_agreement_number'),
    ]

    def remove_placeholder_values(apps, schema_editor):
        Customer = apps.get_model('structure', 'Customer')
        Customer.objects.filter(agreement_number=MARKER).update(agreement_number='')

    operations = [migrations.RunPython(remove_placeholder_values)]
