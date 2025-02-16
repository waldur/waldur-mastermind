from typing import Literal


class CoreStates:
    CREATION_SCHEDULED = 5
    CREATING = 6
    UPDATE_SCHEDULED = 1
    UPDATING = 2
    DELETION_SCHEDULED = 7
    DELETING = 8
    OK = 3
    ERRED = 4

    CHOICES = (
        (CREATION_SCHEDULED, "Creation Scheduled"),
        (CREATING, "Creating"),
        (UPDATE_SCHEDULED, "Update Scheduled"),
        (UPDATING, "Updating"),
        (DELETION_SCHEDULED, "Deletion Scheduled"),
        (DELETING, "Deleting"),
        (OK, "OK"),
        (ERRED, "Erred"),
    )

    VALUES = [val for (_, val) in CHOICES]


CoreStateType = Literal[
    "Creation Scheduled",
    "Creating",
    "Update Scheduled",
    "Updating",
    "Deletion Scheduled",
    "Deleting",
    "OK",
    "Erred",
]
