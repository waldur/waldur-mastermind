# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
from django.conf import settings


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0038_add_project_and_customer_permissions'),
        #('users', '0004_migrate_to_new_permissions_model'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='customerrole',
            unique_together=set([]),
        ),
        migrations.RemoveField(
            model_name='customerrole',
            name='customer',
        ),
        migrations.RemoveField(
            model_name='customerrole',
            name='permission_group',
        ),
        migrations.RemoveField(
            model_name='projectgroup',
            name='customer',
        ),
        migrations.RemoveField(
            model_name='projectgroup',
            name='projects',
        ),
        migrations.AlterUniqueTogether(
            name='projectgrouprole',
            unique_together=set([]),
        ),
        migrations.RemoveField(
            model_name='projectgrouprole',
            name='permission_group',
        ),
        migrations.RemoveField(
            model_name='projectgrouprole',
            name='project_group',
        ),
        migrations.AlterUniqueTogether(
            name='projectrole',
            unique_together=set([]),
        ),
        migrations.RemoveField(
            model_name='projectrole',
            name='permission_group',
        ),
        migrations.RemoveField(
            model_name='projectrole',
            name='project',
        ),
        migrations.DeleteModel(
            name='CustomerRole',
        ),
        migrations.DeleteModel(
            name='ProjectGroup',
        ),
        migrations.DeleteModel(
            name='ProjectGroupRole',
        ),
        migrations.DeleteModel(
            name='ProjectRole',
        ),
    ]
