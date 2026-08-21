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
    created_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
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
    # Pub/sub state lives on the generic EventConsumer (waldur_core.logging), not
    # on this site-agent model. A site agent owns at most one consumer.
    event_consumer = models.OneToOneField(
        "logging.EventConsumer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_identity",
    )

    class Meta:
        verbose_name = _("Agent identity")
        ordering = ["created", "id"]

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
        ordering = ["created", "id"]
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
        ordering = ["created", "id"]
        unique_together = ("service", "name")

    def __str__(self) -> str:
        return f"{self.service.name} - {self.name}"


class SiteAgentLog(core_models.UuidMixin, TimeStampedModel):
    """Log entry shipped from a Waldur Site Agent."""

    agent_identity = models.ForeignKey(
        AgentIdentity,
        on_delete=models.CASCADE,
        related_name="logs",
    )
    timestamp = models.FloatField(
        help_text=_("Unix timestamp of the log entry"),
        db_index=True,
    )
    level = models.CharField(
        max_length=20,
        choices=enums.LogLevel.CHOICES,
        db_index=True,
    )
    message = models.TextField()
    module = models.CharField(max_length=255)

    class Permissions:
        customer_path = "agent_identity__offering__customer"

    class Meta:
        verbose_name = _("Site agent log")
        ordering = ["-timestamp", "id"]
