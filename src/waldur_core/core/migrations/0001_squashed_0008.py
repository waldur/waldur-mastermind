# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import re

import django.contrib.auth.models
import django.core.validators
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.core.models


class Migration(migrations.Migration):
    replaces = [('core', '0001_initial'), ('core', '0002_user_organization_approved'),
                ('core', '0003_ssh_key_name_length_changed'),
                ('core', '0001_squashed_0003_ssh_key_name_length_changed'),
                ('core', '0002_enlarge_civil_number_user_field'), ('core', '0003_user_registration_method'),
                ('core', '0004_user_agreement_date'), ('core', '0005_add_user_language_and_competence'),
                ('core', '0006_user_is_support'), ('core', '0007_user_token_lifetime'),
                ('core', '0008_sshpublickey_is_shared')]

    initial = True

    dependencies = [
        ('auth', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='User',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('password', models.CharField(max_length=128, verbose_name='password')),
                ('last_login', models.DateTimeField(blank=True, null=True, verbose_name='last login')),
                ('is_superuser', models.BooleanField(default=False,
                                                     help_text='Designates that this user has all permissions without explicitly assigning them.',
                                                     verbose_name='superuser status')),
                ('description', models.CharField(blank=True, max_length=500, verbose_name='description')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('username', models.CharField(
                    help_text='Required. 30 characters or fewer. Letters, numbers and @/./+/-/_ characters',
                    max_length=30, unique=True, validators=[
                        django.core.validators.RegexValidator(re.compile('^[\\w.@+-]+$'), 'Enter a valid username.',
                                                              'invalid')], verbose_name='username')),
                ('civil_number', models.CharField(blank=True, default=None, max_length=50, null=True, unique=True,
                                                  verbose_name='civil number')),
                ('full_name', models.CharField(blank=True, max_length=100, verbose_name='full name')),
                ('native_name', models.CharField(blank=True, max_length=100, verbose_name='native name')),
                ('phone_number', models.CharField(blank=True, max_length=255, verbose_name='phone number')),
                ('organization', models.CharField(blank=True, max_length=80, verbose_name='organization')),
                ('organization_approved',
                 models.BooleanField(default=False, help_text='Designates whether user organization was approved.',
                                     verbose_name='organization approved')),
                ('job_title', models.CharField(blank=True, max_length=40, verbose_name='job title')),
                ('email', models.EmailField(blank=True, max_length=75, verbose_name='email address')),
                ('is_staff', models.BooleanField(default=False,
                                                 help_text='Designates whether the user can log into this admin site.',
                                                 verbose_name='staff status')),
                ('is_active', models.BooleanField(default=True,
                                                  help_text='Designates whether this user should be treated as active. Unselect this instead of deleting accounts.',
                                                  verbose_name='active')),
                ('is_support',
                 models.BooleanField(default=False, help_text='Designates whether the user is a global support user.',
                                     verbose_name='support status')),
                ('date_joined', models.DateTimeField(default=django.utils.timezone.now, verbose_name='date joined')),
                ('registration_method', models.CharField(blank=True, default='default',
                                                         help_text='Indicates what registration method were used.',
                                                         max_length=50, verbose_name='registration method')),
                ('agreement_date',
                 models.DateTimeField(blank=True, help_text='Indicates when the user has agreed with the policy.',
                                      null=True, verbose_name='agreement date')),
                ('preferred_language', models.CharField(blank=True, max_length=10)),
                ('competence', models.CharField(blank=True, max_length=255)),
                ('token_lifetime', models.PositiveIntegerField(help_text='Token lifetime in seconds.', null=True,
                                                               validators=[
                                                                   django.core.validators.MinValueValidator(60)])),
                ('groups', models.ManyToManyField(blank=True,
                                                  help_text='The groups this user belongs to. A user will get all permissions granted to each of their groups.',
                                                  related_name='user_set', related_query_name='user', to='auth.Group',
                                                  verbose_name='groups')),
                ('user_permissions', models.ManyToManyField(blank=True, help_text='Specific permissions for this user.',
                                                            related_name='user_set', related_query_name='user',
                                                            to='auth.Permission', verbose_name='user permissions')),
            ],
            options={
                'verbose_name': 'user',
                'verbose_name_plural': 'users',
            },
            bases=(waldur_core.logging.loggers.LoggableMixin, models.Model),
            managers=[
                ('objects', django.contrib.auth.models.UserManager()),
            ],
        ),
        migrations.CreateModel(
            name='SshPublicKey',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('name', models.CharField(blank=True, max_length=150)),
                ('fingerprint', models.CharField(max_length=47)),
                ('public_key', models.TextField(validators=[django.core.validators.MaxLengthValidator(2000),
                                                            waldur_core.core.models.validate_ssh_public_key])),
                ('is_shared', models.BooleanField(default=False)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'SSH public key',
                'verbose_name_plural': 'SSH public keys',
            },
            bases=(waldur_core.logging.loggers.LoggableMixin, models.Model),
        ),
        migrations.AlterUniqueTogether(
            name='sshpublickey',
            unique_together=set([('user', 'name')]),
        ),
    ]
