# Generated by Django 2.2.13 on 2021-03-01 09:38

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0043_category_default_tenant_category'),
    ]

    operations = [
        migrations.RemoveField(model_name='order', name='_file',),
    ]
