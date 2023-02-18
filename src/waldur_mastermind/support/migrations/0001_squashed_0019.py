import django.db.models.deletion
import django.utils.timezone
import django_fsm
import model_utils.fields
from django.conf import settings
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.core.models
import waldur_core.core.validators
import waldur_core.structure.models


class Migration(migrations.Migration):
    replaces = [
        ('support', '0001_squashed_0037'),
        ('support', '0002_nullable_issue_caller'),
        ('support', '0003_cascade_delete_support_customer'),
        ('support', '0004_templateconfirmationcomment'),
        ('support', '0005_extend_icon_url_size'),
        ('support', '0006_feedback'),
        ('support', '0007_extended_evaluation'),
        ('support', '0008_offering_backend_id'),
        ('support', '0009_extend_description_limits'),
        ('support', '0010_error_traceback'),
        ('support', '0011_drop_offering'),
        ('support', '0012_issue_feedback_request'),
        ('support', '0013_score_on_a_ten_point_system'),
        ('support', '0014_unique_issue_attachment'),
        ('support', '0015_fill_attachment_mime_type'),
        ('support', '0016_requesttype_fields'),
        ('support', '0017_alter_requesttype_fields'),
        ('support', '0018_issue_remote_id'),
        ('support', '0019_comment_remote_id'),
    ]

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('structure', '0001_squashed_0054'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [
        migrations.CreateModel(
            name='IgnoredIssueStatus',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        unique=True,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name='IssueStatus',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        help_text='Status name in Jira.', max_length=255, unique=True
                    ),
                ),
                (
                    'type',
                    django_fsm.FSMIntegerField(
                        choices=[(0, 'Resolved'), (1, 'Canceled')], default=0
                    ),
                ),
            ],
            options={
                'verbose_name': 'Issue status',
                'verbose_name_plural': 'Issue statuses',
            },
        ),
        migrations.CreateModel(
            name='RequestType',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.IntegerField(unique=True)),
                ('issue_type_name', models.CharField(max_length=255)),
                ('fields', models.JSONField(blank=True, default=dict)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='SupportUser',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                (
                    'backend_id',
                    models.CharField(
                        blank=True, max_length=255, null=True, unique=True
                    ),
                ),
                (
                    'is_active',
                    models.BooleanField(
                        default=True,
                        help_text='Designates whether this user should be treated as active. Unselect this instead of deleting accounts.',
                        verbose_name='active',
                    ),
                ),
                (
                    'user',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='Template',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'created',
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='created',
                    ),
                ),
                (
                    'modified',
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='modified',
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('native_name', models.CharField(blank=True, max_length=150)),
                ('description', models.TextField()),
                ('native_description', models.TextField(blank=True)),
                (
                    'issue_type',
                    models.CharField(
                        choices=[
                            ('INFORMATIONAL', 'Informational'),
                            ('SERVICE_REQUEST', 'Service request'),
                            ('CHANGE_REQUEST', 'Change request'),
                            ('INCIDENT', 'Incident'),
                        ],
                        default='INFORMATIONAL',
                        max_length=30,
                    ),
                ),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='TemplateAttachment',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'created',
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='created',
                    ),
                ),
                (
                    'modified',
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='modified',
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('file', models.FileField(upload_to='support_template_attachments')),
                (
                    'template',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='attachments',
                        to='support.template',
                    ),
                ),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='TemplateStatusNotification',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'status',
                    models.CharField(
                        max_length=255,
                        unique=True,
                        validators=[waldur_core.core.validators.validate_name],
                    ),
                ),
                (
                    'html',
                    models.TextField(
                        validators=[
                            waldur_core.core.validators.validate_name,
                            waldur_core.core.validators.validate_template_syntax,
                        ]
                    ),
                ),
                (
                    'text',
                    models.TextField(
                        validators=[
                            waldur_core.core.validators.validate_name,
                            waldur_core.core.validators.validate_template_syntax,
                        ]
                    ),
                ),
                (
                    'subject',
                    models.CharField(
                        max_length=255,
                        validators=[
                            waldur_core.core.validators.validate_name,
                            waldur_core.core.validators.validate_template_syntax,
                        ],
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name='Issue',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'created',
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='created',
                    ),
                ),
                (
                    'modified',
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='modified',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                (
                    'state',
                    django_fsm.FSMIntegerField(
                        choices=[
                            (5, 'Creation Scheduled'),
                            (6, 'Creating'),
                            (1, 'Update Scheduled'),
                            (2, 'Updating'),
                            (7, 'Deletion Scheduled'),
                            (8, 'Deleting'),
                            (3, 'OK'),
                            (4, 'Erred'),
                        ],
                        default=5,
                    ),
                ),
                (
                    'backend_id',
                    models.CharField(
                        blank=True, max_length=255, null=True, unique=True
                    ),
                ),
                ('key', models.CharField(blank=True, max_length=255)),
                ('type', models.CharField(max_length=255)),
                (
                    'link',
                    models.URLField(
                        blank=True,
                        help_text='Link to issue in support system.',
                        max_length=255,
                    ),
                ),
                ('summary', models.CharField(max_length=255)),
                ('description', models.TextField(blank=True)),
                ('deadline', models.DateTimeField(blank=True, null=True)),
                ('impact', models.CharField(blank=True, max_length=255)),
                ('status', models.CharField(max_length=255)),
                ('resolution', models.CharField(blank=True, max_length=255)),
                ('priority', models.CharField(blank=True, max_length=255)),
                ('resource_object_id', models.PositiveIntegerField(null=True)),
                ('first_response_sla', models.DateTimeField(blank=True, null=True)),
                ('resolution_date', models.DateTimeField(blank=True, null=True)),
                (
                    'assignee',
                    models.ForeignKey(
                        blank=True,
                        help_text='Help desk user who will implement the issue',
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='issues',
                        to='support.supportuser',
                    ),
                ),
                (
                    'caller',
                    models.ForeignKey(
                        blank=True,
                        help_text='Waldur user who has reported the issue.',
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='created_issues',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    'customer',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='issues',
                        to='structure.customer',
                        verbose_name='organization',
                    ),
                ),
                (
                    'project',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='issues',
                        to='structure.project',
                    ),
                ),
                (
                    'reporter',
                    models.ForeignKey(
                        blank=True,
                        help_text='Help desk user who have created the issue that is reported by caller.',
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='reported_issues',
                        to='support.supportuser',
                    ),
                ),
                (
                    'resource_content_type',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to='contenttypes.contenttype',
                    ),
                ),
                (
                    'template',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='issues',
                        to='support.template',
                    ),
                ),
                ('error_traceback', models.TextField(blank=True)),
                (
                    'feedback_request',
                    models.BooleanField(
                        blank=True,
                        default=True,
                        help_text='Request feedback from the issue creator after resolution of the issue',
                    ),
                ),
                (
                    'remote_id',
                    models.CharField(
                        blank=True, max_length=255, null=True, unique=True
                    ),
                ),
            ],
            options={
                'ordering': ['-created'],
            },
            bases=(
                waldur_core.structure.models.StructureLoggableMixin,
                waldur_core.core.models.BackendModelMixin,
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name='SupportCustomer',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('backend_id', models.CharField(max_length=255, unique=True)),
                (
                    'user',
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name='TemplateConfirmationComment',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'issue_type',
                    models.CharField(default='default', max_length=255, unique=True),
                ),
                (
                    'template',
                    models.TextField(
                        validators=[
                            waldur_core.core.validators.validate_name,
                            waldur_core.core.validators.validate_template_syntax,
                        ]
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name='Priority',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'description',
                    models.CharField(
                        blank=True, max_length=2000, verbose_name='description'
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                (
                    'icon_url',
                    models.URLField(
                        blank=True, max_length=500, verbose_name='icon url'
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.CharField(blank=True, max_length=255)),
            ],
            options={
                'verbose_name': 'Priority',
                'verbose_name_plural': 'Priorities',
            },
        ),
        migrations.CreateModel(
            name='Feedback',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'created',
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='created',
                    ),
                ),
                (
                    'modified',
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='modified',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                (
                    'state',
                    django_fsm.FSMIntegerField(
                        choices=[
                            (5, 'Creation Scheduled'),
                            (6, 'Creating'),
                            (1, 'Update Scheduled'),
                            (2, 'Updating'),
                            (7, 'Deletion Scheduled'),
                            (8, 'Deleting'),
                            (3, 'OK'),
                            (4, 'Erred'),
                        ],
                        default=5,
                    ),
                ),
                (
                    'evaluation',
                    models.SmallIntegerField(
                        choices=[
                            (1, '1'),
                            (2, '2'),
                            (3, '3'),
                            (4, '4'),
                            (5, '5'),
                            (6, '6'),
                            (7, '7'),
                            (8, '8'),
                            (9, '9'),
                            (10, '10'),
                        ]
                    ),
                ),
                ('comment', models.TextField(blank=True)),
                (
                    'issue',
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE, to='support.issue'
                    ),
                ),
                ('error_traceback', models.TextField(blank=True)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='Attachment',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'created',
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='created',
                    ),
                ),
                (
                    'modified',
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='modified',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                (
                    'state',
                    django_fsm.FSMIntegerField(
                        choices=[
                            (5, 'Creation Scheduled'),
                            (6, 'Creating'),
                            (1, 'Update Scheduled'),
                            (2, 'Updating'),
                            (7, 'Deletion Scheduled'),
                            (8, 'Deleting'),
                            (3, 'OK'),
                            (4, 'Erred'),
                        ],
                        default=5,
                    ),
                ),
                ('file', models.FileField(upload_to='support_attachments')),
                ('backend_id', models.CharField(max_length=255)),
                (
                    'mime_type',
                    models.CharField(
                        blank=True, max_length=100, verbose_name='MIME type'
                    ),
                ),
                (
                    'file_size',
                    models.PositiveIntegerField(
                        blank=True, null=True, verbose_name='Filesize, B'
                    ),
                ),
                (
                    'thumbnail',
                    models.FileField(
                        blank=True,
                        null=True,
                        upload_to='support_attachments_thumbnails',
                    ),
                ),
                (
                    'author',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='attachments',
                        to='support.supportuser',
                    ),
                ),
                (
                    'issue',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='attachments',
                        to='support.issue',
                    ),
                ),
                ('error_traceback', models.TextField(blank=True)),
            ],
            options={
                'abstract': False,
                'unique_together': {('issue', 'backend_id')},
            },
            bases=(waldur_core.structure.models.StructureLoggableMixin, models.Model),
        ),
        migrations.CreateModel(
            name='Comment',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'created',
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='created',
                    ),
                ),
                (
                    'modified',
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='modified',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                (
                    'state',
                    django_fsm.FSMIntegerField(
                        choices=[
                            (5, 'Creation Scheduled'),
                            (6, 'Creating'),
                            (1, 'Update Scheduled'),
                            (2, 'Updating'),
                            (7, 'Deletion Scheduled'),
                            (8, 'Deleting'),
                            (3, 'OK'),
                            (4, 'Erred'),
                        ],
                        default=5,
                    ),
                ),
                ('description', models.TextField()),
                ('is_public', models.BooleanField(default=True)),
                ('backend_id', models.CharField(blank=True, max_length=255, null=True)),
                (
                    'author',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='comments',
                        to='support.supportuser',
                    ),
                ),
                (
                    'issue',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='comments',
                        to='support.issue',
                    ),
                ),
                ('error_traceback', models.TextField(blank=True)),
                ('remote_id', models.CharField(blank=True, max_length=255, null=True)),
            ],
            options={
                'ordering': ['-created'],
                'unique_together': {('backend_id', 'issue')},
            },
            bases=(waldur_core.core.models.BackendModelMixin, models.Model),
        ),
    ]
