from django.db import models


class InvitationState(models.TextChoices):
    PENDING_PROJECT = "project", "Pending project"
    REQUESTED = "requested", "Requested"
    REJECTED = "rejected", "Rejected"
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    CANCELED = "canceled", "Canceled"
    EXPIRED = "expired", "Expired"
