# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations
import re
import waldur_core.core.models
import waldur_core.core.fields
import django.utils.timezone
from django.conf import settings
import django.contrib.auth.models
import django.core.validators


class Migration(migrations.Migration):

    #replaces = [('core', '0001_initial'), ('core', '0002_user_organization_approved'), ('core', '0003_ssh_key_name_length_changed')]

    dependencies = [
        ('auth', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='User',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('password', models.CharField(max_length=128, verbose_name='password')),
                ('last_login', models.DateTimeField(null=True, verbose_name='last login', blank=True)),
                ('is_superuser', models.BooleanField(default=False, help_text='Designates that this user has all permissions without explicitly assigning them.', verbose_name='superuser status')),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('username', models.CharField(help_text='Required. 30 characters or fewer. Letters, numbers and @/./+/-/_ characters', unique=True, max_length=30, verbose_name='username', validators=[django.core.validators.RegexValidator(re.compile('^[\\w.@+-]+$'), 'Enter a valid username.', 'invalid')])),
                ('civil_number', models.CharField(null=True, default=None, max_length=10, blank=True, unique=True, verbose_name='civil number')),
                ('full_name', models.CharField(max_length=100, verbose_name='full name', blank=True)),
                ('native_name', models.CharField(max_length=100, verbose_name='native name', blank=True)),
                ('phone_number', models.CharField(max_length=40, verbose_name='phone number', blank=True)),
                ('organization', models.CharField(max_length=80, verbose_name='organization', blank=True)),
                ('job_title', models.CharField(max_length=40, verbose_name='job title', blank=True)),
                ('email', models.EmailField(max_length=75, verbose_name='email address', blank=True)),
                ('is_staff', models.BooleanField(default=False, help_text='Designates whether the user can log into this admin site.', verbose_name='staff status')),
                ('is_active', models.BooleanField(default=True, help_text='Designates whether this user should be treated as active. Unselect this instead of deleting accounts.', verbose_name='active')),
                ('date_joined', models.DateTimeField(default=django.utils.timezone.now, verbose_name='date joined')),
                ('groups', models.ManyToManyField(related_query_name='user', related_name='user_set', to='auth.Group', blank=True, help_text='The groups this user belongs to. A user will get all permissions granted to each of their groups.', verbose_name='groups')),
                ('user_permissions', models.ManyToManyField(related_query_name='user', related_name='user_set', to='auth.Permission', blank=True, help_text='Specific permissions for this user.', verbose_name='user permissions')),
            ],
            options={
                'verbose_name': 'user',
                'verbose_name_plural': 'users',
            },
            managers=[
                ('objects', django.contrib.auth.models.UserManager()),
            ],
            bases=(models.Model,),
        ),
        migrations.CreateModel(
            name='SshPublicKey',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('name', models.CharField(max_length=150, blank=True)),
                ('fingerprint', models.CharField(max_length=47)),
                ('public_key', models.TextField(validators=[django.core.validators.MaxLengthValidator(2000), waldur_core.core.models.validate_ssh_public_key])),
                ('user', models.ForeignKey(to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'unique_together': set([('user', 'name')]),
                'verbose_name': 'SSH public key',
                'verbose_name_plural': 'SSH public keys'
            },
            bases=(models.Model,),
        ),
        migrations.AddField(
            model_name='user',
            name='organization_approved',
            field=models.BooleanField(default=False, help_text='Designates whether user organization was approved.', verbose_name='organization approved'),
            preserve_default=True,
        ),
    ]
