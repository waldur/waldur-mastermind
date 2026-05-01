from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_fsm import FSMField, transition
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.permissions.models import Role
from waldur_mastermind.marketplace import models as marketplace_models

from .enums import KeycloakMembershipState


class OfferingKeycloakGroup(
    core_models.UuidMixin,
    core_models.BackendMixin,
    TimeStampedModel,
):
    name = models.CharField(_("Group name"), max_length=150, blank=True)
    offering = models.ForeignKey(
        marketplace_models.Offering,
        on_delete=models.CASCADE,
        related_name="keycloak_groups",
    )
    role = models.ForeignKey(
        Role,
        on_delete=models.CASCADE,
        related_name="keycloak_groups",
    )
    resource = models.ForeignKey(
        marketplace_models.Resource,
        on_delete=models.CASCADE,
        related_name="keycloak_groups",
        null=True,
        blank=True,
    )
    scope_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=_(
            "Sub-entity identifier within a resource, e.g. Rancher project ID within a cluster."
        ),
    )

    class Meta:
        unique_together = (("offering", "role", "resource", "scope_id"),)
        ordering = ["-created"]

    def __str__(self):
        return self.name


class OfferingKeycloakMembership(
    core_models.UuidMixin,
    TimeStampedModel,
    core_models.ErrorMessageMixin,
):
    username = models.CharField(max_length=255, help_text=_("Keycloak user username"))
    email = models.EmailField(help_text=_("User's email for notifications"))
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    state = FSMField(
        choices=KeycloakMembershipState.CHOICES,
        default=KeycloakMembershipState.PENDING,
    )
    last_checked = models.DateTimeField(auto_now=True)
    group = models.ForeignKey(
        OfferingKeycloakGroup,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        core_models.User,
        on_delete=models.CASCADE,
        related_name="+",
        null=True,
        blank=True,
    )

    class Meta:
        unique_together = (("username", "group"),)
        ordering = ["-created"]

    @transition(
        field=state,
        source=KeycloakMembershipState.PENDING,
        target=KeycloakMembershipState.ACTIVE,
    )
    def activate(self):
        pass

    def refresh_last_checked(self):
        self.last_checked = timezone.now()

    def __str__(self):
        return f"{self.username} in {self.group}"
