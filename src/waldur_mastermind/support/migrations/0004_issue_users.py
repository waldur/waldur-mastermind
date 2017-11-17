# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
from django.conf import settings


class Migration(migrations.Migration):

    dependencies = [
        ('support', '0003_additional_issue_fields'),
    ]

    operations = [
        migrations.AlterField(
            model_name='issue',
            name='assignee',
            field=models.ForeignKey(related_name='issues', blank=True, to='support.SupportUser', help_text='Help desk user who will implement the issue', null=True),
        ),
        migrations.AlterField(
            model_name='issue',
            name='caller',
            field=models.ForeignKey(related_name='created_issues', to=settings.AUTH_USER_MODEL, help_text='Waldur user who has reported the issue.'),
        ),
        migrations.AlterField(
            model_name='issue',
            name='reporter',
            field=models.ForeignKey(related_name='reported_issues', blank=True, to='support.SupportUser', help_text='Help desk user who have created the issue that is reported by caller.', null=True),
        ),
    ]
