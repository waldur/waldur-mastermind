import django.db.models.deletion
import django.utils.timezone
import django_fsm
import model_utils.fields
from django.conf import settings
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.structure.models


class Migration(migrations.Migration):
    replaces = [
        ('users', '0001_squashed_0004'),
        ('users', '0002_add_user_details'),
        ('users', '0003_extend_organization'),
        ('users', '0004_invitation_error_traceback'),
        ('users', '0005_remove_invitation_link_template'),
        ('users', '0006_invitation_affiliations'),
        ('users', '0007_clean_affiliations'),
        ('users', '0008_affiliations_default'),
        ('users', '0009_groupinvitation'),
        ('users', '0010_permissionrequest'),
        ('users', '0011_json_field'),
        ('users', '0012_alter_invitation_job_title'),
    ]

    initial = True

    dependencies = [
        ('structure', '0001_squashed_0036'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='GroupInvitation',
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
                (
                    'customer_role',
                    waldur_core.structure.models.CustomerRole(
                        blank=True,
                        choices=[
                            ('owner', 'Owner'),
                            ('support', 'Support'),
                            ('service_manager', 'Service manager'),
                        ],
                        max_length=30,
                        null=True,
                        verbose_name='organization role',
                    ),
                ),
                (
                    'project_role',
                    waldur_core.structure.models.ProjectRole(
                        blank=True,
                        choices=[
                            ('admin', 'Administrator'),
                            ('manager', 'Manager'),
                            ('member', 'Member'),
                        ],
                        max_length=30,
                        null=True,
                    ),
                ),
                ('is_active', models.BooleanField(default=True)),
                (
                    'created_by',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    'customer',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
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
                        to='structure.project',
                    ),
                ),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='PermissionRequest',
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
                (
                    'state',
                    django_fsm.FSMIntegerField(
                        choices=[
                            (1, 'draft'),
                            (2, 'pending'),
                            (3, 'approved'),
                            (4, 'rejected'),
                            (5, 'canceled'),
                        ],
                        default=1,
                    ),
                ),
                (
                    'reviewed_at',
                    models.DateTimeField(blank=True, editable=False, null=True),
                ),
                ('review_comment', models.TextField(blank=True, null=True)),
                (
                    'created_by',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='+',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    'invitation',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to='users.groupinvitation',
                    ),
                ),
                (
                    'reviewed_by',
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
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='Invitation',
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
                    'customer_role',
                    waldur_core.structure.models.CustomerRole(
                        blank=True,
                        choices=[
                            ('owner', 'Owner'),
                            ('support', 'Support'),
                            ('service_manager', 'Service manager'),
                        ],
                        max_length=30,
                        null=True,
                        verbose_name='organization role',
                    ),
                ),
                (
                    'project_role',
                    waldur_core.structure.models.ProjectRole(
                        blank=True,
                        choices=[
                            ('admin', 'Administrator'),
                            ('manager', 'Manager'),
                            ('member', 'Member'),
                        ],
                        max_length=30,
                        null=True,
                    ),
                ),
                (
                    'state',
                    models.CharField(
                        choices=[
                            ('requested', 'Requested'),
                            ('rejected', 'Rejected'),
                            ('pending', 'Pending'),
                            ('accepted', 'Accepted'),
                            ('canceled', 'Canceled'),
                            ('expired', 'Expired'),
                        ],
                        default='pending',
                        max_length=10,
                    ),
                ),
                (
                    'email',
                    models.EmailField(
                        help_text='Invitation link will be sent to this email. Note that user can accept invitation with different email.',
                        max_length=254,
                    ),
                ),
                (
                    'civil_number',
                    models.CharField(
                        blank=True,
                        help_text='Civil number of invited user. If civil number is not defined any user can accept invitation.',
                        max_length=50,
                    ),
                ),
                (
                    'created_by',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    'customer',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
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
                        to='structure.project',
                    ),
                ),
                (
                    'approved_by',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    'full_name',
                    models.CharField(
                        blank=True, max_length=100, verbose_name='full name'
                    ),
                ),
                (
                    'job_title',
                    models.CharField(
                        blank=True, max_length=120, verbose_name='job title'
                    ),
                ),
                (
                    'native_name',
                    models.CharField(
                        blank=True, max_length=100, verbose_name='native name'
                    ),
                ),
                (
                    'organization',
                    models.CharField(
                        blank=True, max_length=255, verbose_name='organization'
                    ),
                ),
                (
                    'phone_number',
                    models.CharField(
                        blank=True, max_length=255, verbose_name='phone number'
                    ),
                ),
                (
                    'tax_number',
                    models.CharField(
                        blank=True, max_length=50, verbose_name='tax number'
                    ),
                ),
                ('error_traceback', models.TextField(blank=True)),
                (
                    'affiliations',
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="Person's affiliation within organization such as student, faculty, staff.",
                    ),
                ),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
