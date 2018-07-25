# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import waldur_auth_valimo.models
import django.utils.timezone
from django.conf import settings
import django_fsm
import waldur_core.core.fields
import model_utils.fields


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AuthResult',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                ('phone', models.CharField(max_length=30)),
                ('message', models.CharField(default=waldur_auth_valimo.models._default_message, help_text='This message will be shown to user.', max_length=4)),
                ('state', django_fsm.FSMField(default='Scheduled', max_length=50, choices=[('Scheduled', 'Scheduled'), ('Processing', 'Processing'), ('OK', 'OK'), ('Canceled', 'Canceled'), ('Erred', 'Erred')])),
                ('details', models.CharField(help_text='Cancellation details.', max_length=255, blank=True)),
                ('backend_transaction_id', models.CharField(max_length=100, blank=True)),
                ('user', models.ForeignKey(related_name='auth_valimo_results', to=settings.AUTH_USER_MODEL, null=True)),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
