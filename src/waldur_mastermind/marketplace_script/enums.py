from django.db import models


class DryRunStates(models.IntegerChoices):
    PENDING = 1, "pending"
    EXECUTING = 2, "executing"
    DONE = 3, "done"
    ERRED = 4, "erred"


class DryRunTypes(models.IntegerChoices):
    CREATE = 1, "Create"
    UPDATE = 2, "Update"
    TERMINATE = 3, "Terminate"
    RESTORE = 4, "Restore"
    PULL = 5, "Pull"

    @classmethod
    def get_type_display(cls, index):
        try:
            return cls(index).label.lower()
        except ValueError:
            return index
