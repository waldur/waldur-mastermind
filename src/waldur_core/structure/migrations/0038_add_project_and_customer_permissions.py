# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import collections

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import migrations, models
import model_utils.fields
import waldur_core.core.fields
import waldur_core.structure.models
import django.utils.timezone


def migrate_permissions(apps, schema_editor):
    customer_owners, project_managers, project_admins = fetch_users()
    migrate_customer_permissions(apps, customer_owners)
    migrate_project_permissions(apps, project_managers, project_admins)


def fetch_users():
    UserGroup = get_user_model().groups.through

    customer_owners = collections.defaultdict(list)
    project_managers = collections.defaultdict(list)
    project_admins = collections.defaultdict(list)
    arrays = {
        'owner': customer_owners,
        'mgr': project_managers,
        'admin': project_admins,
    }
    user_groups = UserGroup.objects.filter(group__name__startswith='Role:')\
                           .exclude(user=None).select_related('group', 'user')
    for user_group in user_groups:
        parts = user_group.group.name.split(' ')
        if len(parts) != 3:
            continue
        _, uuid, role = parts
        if role in arrays:
            arrays[role][uuid].append(user_group.user)
    return customer_owners, project_managers, project_admins


def migrate_customer_permissions(apps, customer_owners):
    Customer = apps.get_model('structure', 'Customer')
    CustomerPermission = apps.get_model('structure', 'CustomerPermission')

    customers = Customer.objects.filter(uuid__in=customer_owners.keys())
    customers_map = {customer.uuid.hex: customer for customer in customers}

    customer_permissions = [
        CustomerPermission(customer=customer, user_id=user.id, role='owner')
        for uuid, customer in customers_map.items()
        for user in customer_owners[uuid]
    ]
    CustomerPermission.objects.bulk_create(customer_permissions)


def migrate_project_permissions(apps, project_managers, project_admins):
    Project = apps.get_model('structure', 'Project')
    ProjectPermission = apps.get_model('structure', 'ProjectPermission')

    projects = Project.objects.filter(uuid__in=list(project_managers.keys()) + list(project_admins.keys()))
    projects_map = {project.uuid.hex: project for project in projects}

    project_permissions = [
        ProjectPermission(project=project, user_id=user.id, role='manager')
        for uuid, project in projects_map.items()
        for user in project_managers[uuid]
    ] + [
        ProjectPermission(project=project, user_id=user.id, role='admin')
        for uuid, project in projects_map.items()
        for user in project_admins[uuid]
    ]
    ProjectPermission.objects.bulk_create(project_permissions)


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0037_remove_customer_billing_backend_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='CustomerPermission',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False)),
                ('expiration_time', models.DateTimeField(null=True, blank=True)),
                ('is_active', models.BooleanField(default=True, db_index=True)),
                ('role', waldur_core.structure.models.CustomerRole(db_index=True, max_length=30, choices=[('owner', 'Owner')])),
                ('created_by', models.ForeignKey(related_name='+', blank=True, to=settings.AUTH_USER_MODEL, null=True)),
                ('customer', models.ForeignKey(verbose_name='organization', related_name='permissions', to='structure.Customer')),
                ('user', models.ForeignKey(to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='ProjectPermission',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False)),
                ('expiration_time', models.DateTimeField(null=True, blank=True)),
                ('is_active', models.BooleanField(default=True, db_index=True)),
                ('role', waldur_core.structure.models.ProjectRole(db_index=True, max_length=30, choices=[('admin', 'Administrator'), ('manager', 'Manager')])),
                ('created_by', models.ForeignKey(related_name='+', blank=True, to=settings.AUTH_USER_MODEL, null=True)),
                ('project', models.ForeignKey(related_name='permissions', to='structure.Project')),
                ('user', models.ForeignKey(to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AlterUniqueTogether(
            name='projectpermission',
            unique_together=set([('project', 'role', 'user', 'is_active')]),
        ),
        migrations.AlterUniqueTogether(
            name='customerpermission',
            unique_together=set([('customer', 'role', 'user', 'is_active')]),
        ),
        migrations.RunPython(migrate_permissions)
    ]
