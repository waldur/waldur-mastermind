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
        (CREATION_SCHEDULED, "CREATION_SCHEDULED"),
        (CREATING, "CREATING"),
        (UPDATE_SCHEDULED, "UPDATE_SCHEDULED"),
        (UPDATING, "UPDATING"),
        (DELETION_SCHEDULED, "DELETION_SCHEDULED"),
        (DELETING, "DELETING"),
        (OK, "OK"),
        (ERRED, "ERRED"),
    )

    VALUES = [val for (_, val) in CHOICES]


CoreStateType = Literal[
    "CREATION_SCHEDULED",
    "CREATING",
    "UPDATE_SCHEDULED",
    "UPDATING",
    "DELETION_SCHEDULED",
    "DELETING",
    "OK",
    "ERRED",
]
