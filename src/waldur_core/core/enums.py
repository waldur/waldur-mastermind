from django.db import models

# ISO 5218 gender codes - defined here to avoid circular imports with models.py
GENDER_CHOICES = [
    (0, "Not known"),
    (1, "Male"),
    (2, "Female"),
    (9, "Not applicable"),
]


class CoreStates(models.IntegerChoices):
    CREATION_SCHEDULED = 5, "CREATION_SCHEDULED"
    CREATING = 6, "CREATING"
    UPDATE_SCHEDULED = 1, "UPDATE_SCHEDULED"
    UPDATING = 2, "UPDATING"
    DELETION_SCHEDULED = 7, "DELETION_SCHEDULED"
    DELETING = 8, "DELETING"
    OK = 3, "OK"
    ERRED = 4, "ERRED"


class ReviewStates:
    DRAFT = 1
    PENDING = 2
    APPROVED = 3
    REJECTED = 4
    CANCELED = 5

    CHOICES = (
        (DRAFT, "draft"),
        (PENDING, "pending"),
        (APPROVED, "approved"),
        (REJECTED, "rejected"),
        (CANCELED, "canceled"),
    )
