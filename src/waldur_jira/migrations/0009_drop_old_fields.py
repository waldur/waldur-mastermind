# Generated by Django 1.11.7 on 2018-01-03 13:39
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('waldur_jira', '0008_unique_props'),
    ]

    operations = [
        migrations.RemoveField(model_name='project', name='available_for_all',),
        migrations.RemoveField(model_name='project', name='reporter_field',),
    ]