from django.db import models
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.core.mixins import ProjectNameTemplateMixin
from waldur_core.permissions.models import Role
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models


class Rule(
    TimeStampedModel,
    core_models.UuidMixin,
    core_models.NameMixin,
    ProjectNameTemplateMixin,
    core_models.UserDetailsMatchMixin,
):
    class Permissions:
        customer_path = "customer"

    customer = models.ForeignKey(
        structure_models.Customer, on_delete=models.CASCADE, null=True
    )
    plan = models.ForeignKey(
        marketplace_models.Plan,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    plan_attributes = models.JSONField(blank=True, default=dict)
    plan_limits = models.JSONField(blank=True, default=dict)
    project_role = models.ForeignKey(
        to=Role,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    customer_role = models.ForeignKey(
        to=Role,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="autoprovisioning_customer_rules",
        help_text="Role granted on the organization itself. Leave empty to grant "
        "no organization-level role.",
    )
    create_project = models.BooleanField(
        default=True,
        help_text="Create (or join) a project for the matched user. Disable to "
        "grant only the organization-level role.",
    )
    user_claims = models.JSONField(
        default=dict,
        blank=True,
        help_text="Identity provider claims the user must carry, as "
        '{"claim": ["accepted", "values"]}. All claims must match; within one '
        "claim any value matches. A value ending in '*' matches by prefix.",
    )
    revoke_when_unmatched = models.BooleanField(
        default=False,
        help_text="Revoke the roles this rule granted once the user stops "
        "matching it. Off by default so enabling a rule cannot silently strip "
        "access that is already in use.",
    )
    use_user_organization_as_customer_name = models.BooleanField(default=False)

    @property
    def grant_source(self) -> str:
        """Provenance token written to ``UserRole.source`` for this rule's grants."""
        return f"rule:{self.uuid.hex}"

    @classmethod
    def get_url_name(cls):
        return "autoprovisioning-rule"
