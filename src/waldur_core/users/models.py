from typing import cast

from django.conf import settings
from django.db import models, transaction
from django.db.models.query_utils import Q
from django.utils.translation import gettext_lazy as _
from django_fsm import FSMField, transition
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel
from model_utils.tracker import FieldInstanceTracker

from waldur_core.core import mixins as core_mixins
from waldur_core.core import models as core_models
from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import add_user
from waldur_core.structure.models import Customer
from waldur_core.structure.signals import permissions_request_approved
from waldur_core.users.enums import InvitationState


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

    customer = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Customer,
    )
    role = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Role,
    )


class GroupInvitation(BaseInvitation):
    is_active = models.BooleanField(default=True)

    # New fields for project creation alternative
    auto_create_project = models.BooleanField(
        default=False,
        help_text="Create project and grant project permissions instead of customer permissions",
    )
    project_name_template = models.CharField(
        max_length=255,
        blank=True,
        help_text="Template for project name. Supports {username}, {email}, {full_name} variables",
    )
    project_role = models.ForeignKey(
        to=Role,
        on_delete=models.CASCADE,
        related_name="group_project_invitations",
        null=True,
        blank=True,
        help_text="Role to grant in the auto-created project. If not specified, uses invitation.role",
    )

    class Permissions:
        customer_path = "customer"

    def get_expiration_time(self):
        return self.created + settings.WALDUR_CORE["GROUP_INVITATION_LIFETIME"]

    def cancel(self):
        self.is_active = False
        self.save(update_fields=["is_active"])

    def __str__(self):
        return f"{self.scope} {self.role.description}"


class Invitation(
    BaseInvitation,
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
        max_length=10, choices=InvitationState.CHOICES, default=InvitationState.PENDING
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
    extra_invitation_text = models.TextField(blank=True, max_length=250)

    def get_expiration_time(self):
        return self.created + settings.WALDUR_CORE["INVITATION_LIFETIME"]

    def accept(self, user):
        add_user(self.scope, user, self.role, self.created_by)

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

    invitation = models.ForeignKey(on_delete=models.PROTECT, to=GroupInvitation)

    created_by = models.ForeignKey[core_models.User](
        on_delete=models.CASCADE,
        to=settings.AUTH_USER_MODEL,
        related_name="+",
    )

    @transaction.atomic
    def approve(self, user, comment=None):
        super().approve(user, comment)

        if self.invitation.auto_create_project:
            # Create project and grant project permission instead of customer permission
            project = self._create_project_for_user(user)
            permission = add_user(
                project,
                self.created_by,
                self.invitation.project_role or self.invitation.role,
                created_by=user,
            )
            scope = project
        else:
            # Original behavior - grant customer/scope permission
            permission = add_user(
                self.invitation.scope,
                self.created_by,
                self.invitation.role,
                created_by=user,
            )
            scope = self.invitation.scope

        permissions_request_approved.send(
            sender=self.__class__,
            permission=permission,
            structure=scope,
        )

    def _create_project_for_user(self, approving_user):
        from waldur_core.structure.models import Project

        project_name = self._resolve_project_name()

        # Use get_or_create to ensure only one project per user per customer
        project, created = Project.objects.get_or_create(
            name=project_name,
            customer=self.invitation.customer,
        )

        return project

    def _resolve_project_name(self):
        if self.invitation.project_name_template:
            return self.invitation.project_name_template.format(
                username=self.created_by.username,
                email=self.created_by.email,
                full_name=self.created_by.get_full_name() or self.created_by.username,
            )
        return f"{self.created_by.username}_project"

    tracker = cast(FieldInstanceTracker, FieldTracker())
