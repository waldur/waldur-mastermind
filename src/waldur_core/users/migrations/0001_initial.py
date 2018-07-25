# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.utils.timezone
import model_utils.fields
import waldur_core.core.fields


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0037_remove_customer_billing_backend_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='Invitation',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('state', models.CharField(default='pending', max_length=8, choices=[('accepted', 'Accepted'), ('canceled', 'Canceled'), ('pending', 'Pending'), ('expired', 'Expired')])),
                ('link_template', models.CharField(help_text='The template must include {uuid} parameter e.g. http://example.com/invitation/{uuid}', max_length=255)),
                ('email', models.EmailField(help_text='Invitation link will be sent to this email. Note that user can accept invitation with different email.', max_length=254)),
                ('customer', models.ForeignKey(verbose_name='organization', related_name='invitations', to='structure.Customer')),
                ('customer_role', models.ForeignKey(related_name='invitations', blank=True, to='structure.CustomerRole', null=True)),
                ('project_role', models.ForeignKey(related_name='invitations', blank=True, to='structure.ProjectRole', null=True)),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
