# Generated by Django 1.11.21 on 2019-06-27 11:46
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('waldur_vmware', '0004_template'),
    ]

    operations = [
        migrations.AddField(
            model_name='virtualmachine',
            name='template',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to='waldur_vmware.Template',
            ),
        ),
    ]