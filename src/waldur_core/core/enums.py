from django.db import models


class CoreStates(models.IntegerChoices):
    CREATION_SCHEDULED = 5, "CREATION_SCHEDULED"
    CREATING = 6, "CREATING"
    UPDATE_SCHEDULED = 1, "UPDATE_SCHEDULED"
    UPDATING = 2, "UPDATING"
    DELETION_SCHEDULED = 7, "DELETION_SCHEDULED"
    DELETING = 8, "DELETING"
    OK = 3, "OK"
    ERRED = 4, "ERRED"


class RuntimeStates:
    ONLINE = "online"
    OFFLINE = "offline"


class ReviewStates(models.IntegerChoices):
    """Review states for requests and workflows requiring approval."""

    DRAFT = 1, "draft"
    PENDING = 2, "pending"
    APPROVED = 3, "approved"
    REJECTED = 4, "rejected"
    CANCELED = 5, "canceled"
