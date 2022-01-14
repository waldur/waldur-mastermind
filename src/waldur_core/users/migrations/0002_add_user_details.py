# Generated by Django 1.11.20 on 2019-04-12 09:20
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('users', '0001_squashed_0004'),
    ]

    operations = [
        migrations.AddField(
            model_name='invitation',
            name='approved_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='invitation',
            name='full_name',
            field=models.CharField(
                blank=True, max_length=100, verbose_name='full name'
            ),
        ),
        migrations.AddField(
            model_name='invitation',
            name='job_title',
            field=models.CharField(blank=True, max_length=40, verbose_name='job title'),
        ),
        migrations.AddField(
            model_name='invitation',
            name='native_name',
            field=models.CharField(
                blank=True, max_length=100, verbose_name='native name'
            ),
        ),
        migrations.AddField(
            model_name='invitation',
            name='organization',
            field=models.CharField(
                blank=True, max_length=80, verbose_name='organization'
            ),
        ),
        migrations.AddField(
            model_name='invitation',
            name='phone_number',
            field=models.CharField(
                blank=True, max_length=255, verbose_name='phone number'
            ),
        ),
        migrations.AddField(
            model_name='invitation',
            name='tax_number',
            field=models.CharField(
                blank=True, max_length=50, verbose_name='tax number'
            ),
        ),
        migrations.AlterField(
            model_name='invitation',
            name='state',
            field=models.CharField(
                choices=[
                    ('requested', 'Requested'),
                    ('rejected', 'Rejected'),
                    ('pending', 'Pending'),
                    ('accepted', 'Accepted'),
                    ('canceled', 'Canceled'),
                    ('expired', 'Expired'),
                ],
                default='pending',
                max_length=10,
            ),
        ),
    ]