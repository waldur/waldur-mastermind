# Generated by Django 1.11.18 on 2019-02-01 11:40
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('waldur_azure', '0007_publicip'),
    ]

    operations = [
        migrations.AddField(
            model_name='networkinterface',
            name='public_ip',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to='waldur_azure.PublicIP',
            ),
        ),
    ]
