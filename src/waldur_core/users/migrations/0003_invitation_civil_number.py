# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0002_invitation_error_message'),
    ]

    operations = [
        migrations.AddField(
            model_name='invitation',
            name='civil_number',
            field=models.CharField(help_text='Civil number of invited user. If civil number is not defined any user can accept invitation.', max_length=50, blank=True),
        ),
    ]
