# Generated by Django 2.2.13 on 2021-03-10 15:10

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0046_orderitem_backend_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='componentusage',
            name='backend_id',
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
