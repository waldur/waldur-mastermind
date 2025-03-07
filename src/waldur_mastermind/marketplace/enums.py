class OfferingStates:
    DRAFT = 1
    ACTIVE = 2
    PAUSED = 3
    ARCHIVED = 4

    CHOICES = (
        (DRAFT, "Draft"),
        (ACTIVE, "Active"),
        (PAUSED, "Paused"),
        (ARCHIVED, "Archived"),
    )

    VALUES = [val for (_, val) in CHOICES]


class RequestTypes:
    CREATE = 1
    UPDATE = 2
    TERMINATE = 3

    CHOICES = (
        (CREATE, "Create"),
        (UPDATE, "Update"),
        (TERMINATE, "Terminate"),
    )

    VALUES = [val for (_, val) in CHOICES]


class CategoryColumnWidget:
    CHOICES = (
        ("csv", "csv"),
        ("filesize", "filesize"),
        ("attached_instance", "attached_instance"),
    )
