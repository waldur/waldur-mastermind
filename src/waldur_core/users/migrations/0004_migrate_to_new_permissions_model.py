# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.conf import settings
from django.db import migrations, models
import waldur_core.structure.models


def migrate_roles(apps, schema_editor):
    Invitation = apps.get_model('users', 'Invitation')
    CUSTOMER_ROLES = {
        0: 'owner'
    }
    PROJECT_ROLES = {
        0: 'admin',
        1: 'manager'
    }
    for invitation in Invitation.objects.all():
        if invitation.customer_role:
            invitation.new_customer_role = CUSTOMER_ROLES[invitation.customer_role.role_type]
        if invitation.project_role:
            invitation.project = invitation.project_role.project
            invitation.new_project_role = PROJECT_ROLES[invitation.project_role.role_type]
        invitation.save()


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0038_add_project_and_customer_permissions'),
        ('users', '0003_invitation_civil_number'),
    ]

    operations = [
        migrations.AddField(
            model_name='invitation',
            name='created_by',
            field=models.ForeignKey(related_name='+', to=settings.AUTH_USER_MODEL, blank=True, null=True),
        ),
        migrations.AddField(
            model_name='invitation',
            name='project',
            field=models.ForeignKey(related_name='invitations', blank=True, to='structure.Project', null=True),
        ),
        migrations.AddField(
            model_name='invitation',
            name='new_customer_role',
            field=waldur_core.structure.models.CustomerRole(verbose_name='organization role', blank=True, max_length=30, null=True, choices=[('owner', 'Owner')]),
        ),
        migrations.AddField(
            model_name='invitation',
            name='new_project_role',
            field=waldur_core.structure.models.ProjectRole(blank=True, max_length=30, null=True, choices=[('admin', 'Administrator'), ('manager', 'Manager')]),
        ),
        migrations.RunPython(migrate_roles),
        migrations.RemoveField(
            model_name='invitation',
            name='customer_role',
        ),
        migrations.RemoveField(
            model_name='invitation',
            name='project_role',
        ),
        migrations.RenameField(
            model_name='invitation',
            old_name='new_customer_role',
            new_name='customer_role',
        ),
        migrations.RenameField(
            model_name='invitation',
            old_name='new_project_role',
            new_name='project_role',
        ),
    ]
