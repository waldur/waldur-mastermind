# Generated by Django 3.2.18 on 2023-03-17 15:17

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('support', '0002_add_backend_name_field'),
    ]

    operations = [
        migrations.AlterField(
            model_name='attachment',
            name='backend_name',
            field=models.CharField(blank=True, default=None, max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name='comment',
            name='backend_name',
            field=models.CharField(blank=True, default=None, max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name='issue',
            name='backend_id',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name='issue',
            name='backend_name',
            field=models.CharField(blank=True, default=None, max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name='supportuser',
            name='backend_id',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name='supportuser',
            name='backend_name',
            field=models.CharField(blank=True, default=None, max_length=255, null=True),
        ),
        migrations.AlterUniqueTogether(
            name='attachment',
            unique_together={('backend_name', 'backend_id')},
        ),
        migrations.AlterUniqueTogether(
            name='comment',
            unique_together={('backend_name', 'backend_id')},
        ),
        migrations.AlterUniqueTogether(
            name='issue',
            unique_together={('backend_name', 'backend_id')},
        ),
        migrations.AlterUniqueTogether(
            name='supportuser',
            unique_together={('backend_name', 'backend_id')},
        ),
    ]