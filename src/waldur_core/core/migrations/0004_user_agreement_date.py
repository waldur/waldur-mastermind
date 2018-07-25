# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_user_registration_method'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='agreement_date',
            field=models.DateTimeField(help_text='Indicates when the user has agreed with the policy.', null=True, verbose_name='agreement date', blank=True),
        ),
    ]
