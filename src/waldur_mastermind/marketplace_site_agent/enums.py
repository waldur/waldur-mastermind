from django.db import models


class AgentServiceState(models.IntegerChoices):
    """Represents the state of an agent service (event processing, usage reporting)."""

    ACTIVE = 1, "Active"
    IDLE = 2, "Idle"
    ERROR = 3, "Error"
