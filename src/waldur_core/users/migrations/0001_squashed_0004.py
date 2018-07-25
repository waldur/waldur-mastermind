# -*- coding: utf-8 -*-

from __future__ import unicode_literals

import django.utils.timezone
import model_utils.fields
from django.conf import settings
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.structure.models


class Migration(migrations.Migration):
    replaces = [('users', '0001_initial'),
                ('users', '0002_invitation_error_message'),
                ('users', '0003_invitation_civil_number'),
                ('users', '0004_migrate_to_new_permissions_model')]

    initial = True

    dependencies = [
        ('structure', '0001_squashed_0054'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Invitation',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False,
                                                                verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False,
                                                                      verbose_name='modified')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                ('customer_role', waldur_core.structure.models.CustomerRole(blank=True, choices=[('owner', 'Owner'), (
                'support', 'Support')], max_length=30, null=True, verbose_name='organization role')),
                ('project_role', waldur_core.structure.models.ProjectRole(blank=True,
                                                                          choices=[('admin', 'Administrator'),
                                                                                   ('manager', 'Manager'),
                                                                                   ('support', 'Support')],
                                                                          max_length=30, null=True)),
                ('state', models.CharField(
                    choices=[('accepted', 'Accepted'), ('canceled', 'Canceled'), ('pending', 'Pending'),
                             ('expired', 'Expired')], default='pending', max_length=8)),
                ('link_template', models.CharField(
                    help_text='The template must include {uuid} parameter e.g. http://example.com/invitation/{uuid}',
                    max_length=255)),
                ('email', models.EmailField(
                    help_text='Invitation link will be sent to this email. Note that user can accept invitation with different email.',
                    max_length=254)),
                ('civil_number', models.CharField(blank=True,
                                                  help_text='Civil number of invited user. If civil number is not defined any user can accept invitation.',
                                                  max_length=50)),
                ('created_by',
                 models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='+',
                                   to=settings.AUTH_USER_MODEL)),
                ('customer', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='invitations',
                                               to='structure.Customer', verbose_name='organization')),
                ('project', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                              related_name='invitations', to='structure.Project')),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
