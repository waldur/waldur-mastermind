from typing import Literal


class BillingTypes:
    FIXED = "fixed"
    USAGE = "usage"
    ONE_TIME = "one"
    ON_PLAN_SWITCH = "few"
    LIMIT = "limit"

    CHOICES = (
        # if billing type is fixed, service provider specifies exact values of amount field of plan component model
        (FIXED, "Fixed-price"),
        # if billing type is usage-based billing is applied when usage report is submitted
        (USAGE, "Usage-based"),
        # if billing type is limit, user specifies limit when resource is provisioned or updated
        (LIMIT, "Limit-based"),
        # if billing type is one-time, billing is applied once on resource activation
        (ONE_TIME, "One-time"),
        # applies fee on resource activation and every time a plan has changed, using pricing of a new plan
        (ON_PLAN_SWITCH, "One-time on plan switch"),
    )


class LimitPeriods:
    MONTH = "month"
    ANNUAL = "annual"
    TOTAL = "total"

    CHOICES = (
        (
            MONTH,
            "Maximum monthly - every month service provider "
            "can report up to the amount requested by user.",
        ),
        (
            ANNUAL,
            "Maximum annually - every year service provider "
            "can report up to the amount requested by user.",
        ),
        (
            TOTAL,
            "Maximum total - SP can report up to the requested "
            "amount over the whole active state of resource.",
        ),
    )


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


class RobotAccountStates:
    REQUESTED = 1
    CREATING = 2
    OK = 3
    REQUESTED_DELETION = 4
    DELETED = 5
    ERROR = 6

    CHOICES = (
        (REQUESTED, "Requested"),
        (CREATING, "Creating"),
        (OK, "OK"),
        (REQUESTED_DELETION, "Requested deletion"),
        (DELETED, "Deleted"),
        (ERROR, "Error"),
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


class OrderStates:
    PENDING_PROJECT = 8
    PENDING_CONSUMER = 1
    PENDING_PROVIDER = 7
    EXECUTING = 2
    DONE = 3
    ERRED = 4
    CANCELED = 5
    REJECTED = 6

    CHOICES = (
        (PENDING_CONSUMER, "pending-consumer"),
        (PENDING_PROVIDER, "pending-provider"),
        (PENDING_PROJECT, "pending-project"),
        (EXECUTING, "executing"),
        (DONE, "done"),
        (ERRED, "erred"),
        (CANCELED, "canceled"),
        (REJECTED, "rejected"),
    )

    TERMINAL_STATES = {DONE, ERRED, CANCELED, REJECTED}
    VALUES = [val for (_, val) in CHOICES]


OrderStatesType = Literal[
    "pending-consumer",
    "pending-provider",
    "pending-project",
    "executing",
    "done",
    "erred",
    "canceled",
    "rejected",
]


class ResourceStates:
    CREATING = 1
    OK = 2
    ERRED = 3
    UPDATING = 4
    TERMINATING = 5
    TERMINATED = 6

    CHOICES = (
        (CREATING, "Creating"),
        (OK, "OK"),
        (ERRED, "Erred"),
        (UPDATING, "Updating"),
        (TERMINATING, "Terminating"),
        (TERMINATED, "Terminated"),
    )
    VALUES = [val for (_, val) in CHOICES]


ResourceStatesType = Literal[
    "Creating",
    "OK",
    "Erred",
    "Updating",
    "Terminating",
    "Terminated",
]
