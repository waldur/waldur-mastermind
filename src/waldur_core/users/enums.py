from django.db import models


class InvitationState(models.TextChoices):
    PENDING_PROJECT = "project", "Pending project"
    REQUESTED = "requested", "Requested"
    REJECTED = "rejected", "Rejected"
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    CANCELED = "canceled", "Canceled"
    EXPIRED = "expired", "Expired"


class MicroAppModes(models.TextChoices):
    """Who a deployed micro-app is offered to."""

    OFF = "off", "Off"
    ALL = "all", "Everyone"
    AUDIENCE = "audience", "Selected users"
