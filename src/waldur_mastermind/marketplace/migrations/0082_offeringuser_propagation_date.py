# Generated by Django 3.2.18 on 2023-03-03 14:39

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0081_robotaccount'),
    ]

    operations = [
        migrations.AddField(
            model_name='offeringuser',
            name='propagation_date',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]