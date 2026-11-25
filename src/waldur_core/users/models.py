from datetime import datetime, time
from typing import cast

from django.conf import settings
from django.core import validators
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models.query_utils import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_fsm import FSMField, transition
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel
from model_utils.tracker import FieldInstanceTracker

from waldur_core.core import mixins as core_mixins
from waldur_core.core import models as core_models
from waldur_core.core.mixins import ProjectNameTemplateMixin
from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import (
    RoleChange,
    add_user,
    has_user,
    validate_role_change,
    validate_role_grant,
)
from waldur_core.structure.models import Customer, OrganizationGroup, Project
from waldur_core.structure.signals import permissions_request_approved
from waldur_core.users.enums import InvitationState, MicroAppModes


class BaseInvitation(core_models.UuidMixin, core_mixins.ScopeMixin, TimeStampedModel):
    class Meta:
        abstract = True

    created_by = models.ForeignKey[core_models.User](
        on_delete=models.CASCADE,
        to=settings.AUTH_USER_MODEL,
        related_name="+",
        blank=True,
        null=True,
    )


class ScopeInvitationMixin(models.Model):
    """Mixin for invitations scoped to a Customer with a system Role."""

    class Meta:
        abstract = True

    customer = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Customer,
    )
    role = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Role,
    )


class GroupInvitation(
    BaseInvitation,
    ScopeInvitationMixin,
    ProjectNameTemplateMixin,
    core_models.UserDetailsMatchMixin,
):
    is_active = models.BooleanField(default=True)
    is_public = models.BooleanField(
        default=False,
        help_text="Allow non-authenticated users to see and accept this invitation. Only staff can create public invitations.",
    )

    # New fields for project creation alternative
    auto_create_project = models.BooleanField(
        default=False,
        help_text="Create project and grant project permissions instead of customer permissions",
    )
    project_role = models.ForeignKey(
        to=Role,
        on_delete=models.CASCADE,
        related_name="group_project_invitations",
        null=True,
        blank=True,
        help_text="Role to grant in the auto-created project. If not specified, uses invitation.role",
    )
    auto_approve = models.BooleanField(
        default=False,
        help_text="Automatically approve permission requests from users matching email patterns or affiliations",
    )
    custom_text = models.TextField(
        blank=True,
        default="",
        max_length=500,
        help_text="Custom description text displayed to users viewing this invitation.",
    )
    allow_multiple_requests = models.BooleanField(
        default=False,
        help_text="Allow users to submit multiple permission requests for this invitation.",
    )
    allow_custom_project_details = models.BooleanField(
        default=False,
        help_text="Allow users to provide custom project name and description when accepting the invitation. "
        "If disabled, the project name is auto-generated from the template.",
    )

    class Permissions:
        customer_path = "customer"

    def clean(self):
        super().clean()
        # Public invitations must use auto_create_project logic
        if self.is_public and not self.auto_create_project:
            raise ValidationError(
                {
                    "auto_create_project": "Public invitations must have auto_create_project enabled."
                }
            )

        # Public invitations should only use project-level roles (since they auto-create projects)
        if (
            self.is_public
            and self.role_id
            and not self.role.name.startswith("PROJECT.")
        ):
            raise ValidationError(
                {
                    "role": "Public invitations can only use project-level roles, not customer-level roles."
                }
            )

    def cancel(self):
        self.is_active = False
        self.save(update_fields=["is_active"])

    def __str__(self):
        return f"{self.scope} {self.role.description}"


class Invitation(
    BaseInvitation,
    ScopeInvitationMixin,
    core_models.ErrorMessageMixin,
    core_models.UserDetailsMixin,
):
    class Permissions:
        customer_path = "customer"

    class ExecutionState:
        SCHEDULED = "Scheduled"
        PROCESSING = "Processing"
        OK = "OK"
        ERRED = "Erred"

        CHOICES = (
            (SCHEDULED, SCHEDULED),
            (PROCESSING, PROCESSING),
            (OK, OK),
            (ERRED, ERRED),
        )

    approved_by = models.ForeignKey[core_models.User](
        on_delete=models.CASCADE,
        to=settings.AUTH_USER_MODEL,
        related_name="+",
        blank=True,
        null=True,
    )

    state = models.CharField(
        max_length=10, choices=InvitationState.choices, default=InvitationState.PENDING
    )
    execution_state = FSMField(
        choices=ExecutionState.CHOICES, default=ExecutionState.SCHEDULED
    )
    email = models.EmailField(
        help_text=_(
            "Invitation link will be sent to this email. Note that user can accept "
            "invitation with different email."
        )
    )
    civil_number = models.CharField(
        max_length=50,
        blank=True,
        help_text=_(
            "Civil number of invited user. If civil number is not defined any user can accept invitation."
        ),
    )
    full_name = models.CharField(_("full name"), max_length=100, blank=True)
    extra_invitation_text = models.TextField(blank=True, max_length=2000)

    def get_expiration_base_datetime(self):
        if not self.scope:
            return self.created

        if isinstance(self.scope, Project) and self.scope.start_date:
            if self.scope.start_date > self.created.date():
                base_date = self.scope.start_date
                base_dt = datetime.combine(base_date, time.min)
                if settings.USE_TZ:
                    return timezone.make_aware(base_dt, timezone.get_current_timezone())
                return base_dt

        return self.created

    def get_expiration_time(self):
        return (
            self.get_expiration_base_datetime()
            + settings.WALDUR_CORE["INVITATION_LIFETIME"]
        )

    def accept(self, user, reason=None, acting_user=None, audit_context=None):
        # The role is granted on the inviter's authority, so the change is
        # checked as theirs.
        validate_role_change(
            self.scope, self.role, self.created_by, RoleChange.GRANT, user=user
        )
        validate_role_grant(self.scope, user, self.role)
        add_user(
            self.scope,
            user,
            self.role,
            self.created_by,
            reason=reason,
            acting_user=acting_user,
            audit_context=audit_context,
        )

        self.state = InvitationState.ACCEPTED
        self.save(update_fields=["state"])

    def cancel(self):
        self.state = InvitationState.CANCELED
        self.save(update_fields=["state"])

    def approve(self, user):
        self.state = InvitationState.PENDING
        self.approved_by = user
        self.save(update_fields=["state", "approved_by"])

    def reject(self):
        self.state = InvitationState.REJECTED
        self.save(update_fields=["state"])

    @transition(
        field=execution_state,
        source="*",
        target=ExecutionState.PROCESSING,
    )
    def begin_processing(self):
        pass

    @transition(
        field=execution_state,
        source=ExecutionState.PROCESSING,
        target=ExecutionState.OK,
    )
    def set_ok(self):
        pass

    @transition(field=execution_state, source="*", target=ExecutionState.ERRED)
    def set_erred(self):
        pass

    def __str__(self):
        return self.email


def filter_own_requests(user):
    return Q(created_by=user)


class PermissionRequest(core_mixins.ReviewMixin, core_models.UuidMixin):
    class Permissions:
        customer_path = "invitation__customer"
        build_query = filter_own_requests

    invitation = models.ForeignKey(on_delete=models.CASCADE, to=GroupInvitation)

    created_by = models.ForeignKey[core_models.User](
        on_delete=models.CASCADE,
        to=settings.AUTH_USER_MODEL,
        related_name="+",
    )
    project_name = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="Custom project name provided by user during invitation acceptance.",
    )
    project_description = models.CharField(
        max_length=2000,
        blank=True,
        default="",
        help_text="Custom project description provided by user during invitation acceptance.",
    )

    @transaction.atomic
    def approve(self, user: core_models.User, comment: str = None):
        super().approve(user, comment)

        created_project = None
        project_created = False
        if self.invitation.auto_create_project:
            # Create project and grant project permission instead of customer permission
            created_project, project_created = self._create_project_for_user(user)
            role = self.invitation.project_role or self.invitation.role
            scope = created_project
        else:
            scope = self.invitation.scope
            role = self.invitation.role

        # Defense-in-depth: skip if user already has the role
        if has_user(scope, self.created_by, role, match_clones=False):
            return {"project": created_project, "project_created": project_created}

        validate_role_change(scope, role, user, RoleChange.GRANT, user=self.created_by)
        validate_role_grant(scope, self.created_by, role)

        permission = add_user(
            scope,
            self.created_by,
            role,
            created_by=user,
        )

        permissions_request_approved.send(
            sender=self.__class__,
            permission=permission,
            structure=scope,
        )

        return {"project": created_project, "project_created": project_created}

    def _create_project_for_user(self, approving_user):
        from waldur_core.structure.models import Project

        project_name = self.project_name or self._resolve_project_name()

        # Use get_or_create with available_objects to exclude soft-deleted projects
        project, created = Project.available_objects.get_or_create(
            name=project_name,
            customer=self.invitation.customer,
            defaults={"description": self.project_description},
        )

        return project, created

    def _resolve_project_name(self):
        return self.invitation.resolve_project_name(self.created_by)

    tracker = cast(FieldInstanceTracker, FieldTracker())


# The name of a micro-app is its directory under homeport's apps/ and its path
# under the portal: lowercase letters, digits and dashes.
MICRO_APP_NAME_REGEX = r"^[a-z0-9][a-z0-9-]{0,99}$"
MICRO_APP_KEY_PREFIX = "micro_apps."


def micro_app_feature_key(name: str) -> str:
    """The FeatureRule key that targets the micro-app ``name``."""
    return f"{MICRO_APP_KEY_PREFIX}{name}"


class FeatureRule(
    core_models.UuidMixin,
    core_models.UserDetailsMatchMixin,
    TimeStampedModel,
):
    """Turns a user-scoped feature flag, or a micro-app, on for matching users.

    A rule matches a user when every criterion group it configures matches:
    the listed users; membership of a listed organization or of an
    organization in a listed group; one of the listed roles in any scope; and
    the identity attributes of UserDetailsMatchMixin plus ``user_claims``, with
    that mixin's own semantics. A rule with no criterion matches no one:
    turning a feature on for everyone is what its global switch is for.
    """

    feature_key = models.CharField(
        max_length=255,
        db_index=True,
        help_text=_(
            "Dotted key of a user-scoped feature flag, or micro_apps.<name> "
            "for a micro-app."
        ),
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    users = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="+"
    )
    customers = models.ManyToManyField(Customer, blank=True, related_name="+")
    organization_groups = models.ManyToManyField(
        OrganizationGroup, blank=True, related_name="+"
    )
    roles = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Role names; the user must hold one of them in any scope."),
    )
    user_claims = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            'Identity provider claims the user must carry, as {"claim": '
            '["accepted", "values"]}. All claims must match; within one claim '
            "any value matches. A value ending in '*' matches by prefix."
        ),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["feature_key", "-created", "uuid"]

    def __str__(self):
        return f"{self.feature_key}: {self.description or self.uuid.hex}"

    @classmethod
    def get_url_name(cls):
        return "feature-rule"


class MicroAppSetting(core_models.UuidMixin, TimeStampedModel):
    """The operator's decision on who a deployed micro-app is offered to.

    Without a row an app follows the global user.show_micro_apps flag, as it
    did before these settings existed.
    """

    Modes = MicroAppModes

    name = models.CharField(
        max_length=100,
        unique=True,
        validators=[validators.RegexValidator(MICRO_APP_NAME_REGEX)],
        help_text=_("The app's directory under apps/ and its path under the portal."),
    )
    mode = models.CharField(max_length=10, choices=Modes.choices, default=Modes.OFF)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name}: {self.mode}"

    @property
    def feature_key(self) -> str:
        return micro_app_feature_key(self.name)

    @classmethod
    def get_url_name(cls):
        return "micro-app-setting"
