from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_fsm import FSMIntegerField
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.core.validators import validate_unix_path
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_site_agent import enums


class AgentIdentity(
    core_models.UuidMixin,
    TimeStampedModel,
    core_models.NameMixin,
    core_models.SlugMixin,
):
    """Identity created for each running Waldur Site Agent."""

    offering = models.ForeignKey(marketplace_models.Offering, on_delete=models.CASCADE)
    version = models.CharField(max_length=100, blank=True, null=True)
    dependencies = models.JSONField(default=list)
    config_file_path = models.CharField(
        _("Config file Path"),
        max_length=150,
        help_text=_("Example: '/etc/waldur/agent.yaml'"),
        blank=True,
        null=True,
        validators=[validate_unix_path],
    )
    config_file_content = models.TextField(blank=True, null=True)
    last_restarted = models.DateTimeField(_("Last restarted at"), default=timezone.now)

    class Meta:
        verbose_name = _("Agent identity")
        ordering = ["created"]

    def __str__(self):
        return self.name


class AgentService(core_models.UuidMixin, TimeStampedModel, core_models.NameMixin):
    """Represents a service running within an agent (event processing, usage reporting)."""

    identity = models.ForeignKey(AgentIdentity, on_delete=models.CASCADE)
    mode = models.CharField(max_length=100, blank=True, null=True)
    state = FSMIntegerField(
        choices=enums.AgentServiceState.CHOICES,
        default=enums.AgentServiceState.ACTIVE,
    )
    statistics = models.JSONField(default=dict)

    class Meta:
        verbose_name = _("Agent service")
        ordering = ["created"]
        unique_together = ("identity", "name")

    def __str__(self) -> str:
        return f"{self.identity.name} - {self.name}"


class AgentProcessor(core_models.UuidMixin, TimeStampedModel, core_models.NameMixin):
    """Represents a processor within an agent service (order processing, membership sync)."""

    service = models.ForeignKey(AgentService, on_delete=models.CASCADE)
    last_run = models.DateTimeField(
        _("Last run"), blank=True, null=True, default=timezone.now
    )
    backend_type = models.CharField(
        _("Backend type"),
        max_length=100,
        help_text=_("Type of the backend, for example SLURM."),
    )
    backend_version = models.CharField(
        _("Backend version"), max_length=100, blank=True, null=True
    )

    class Meta:
        verbose_name = _("Agent processor")
        ordering = ["created"]
        unique_together = ("service", "name")

    def __str__(self) -> str:
        return f"{self.service.name} - {self.name}"
