from typing import Literal

from django.db import models
from django.utils.translation import gettext_lazy as _


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
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    TOTAL = "total"

    CHOICES = (
        (
            MONTH,
            "Maximum monthly - every month service provider "
            "can report up to the amount requested by user.",
        ),
        (
            QUARTERLY,
            "Maximum quarterly - every quarter service provider "
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


class DiscountAggregations:
    # each resource of the offering is discounted on its own component usage
    PER_RESOURCE = "resource"
    # the component usage is summed across all of the customer's resources of
    # the offering and a single discount percentage is applied to the total
    PER_CUSTOMER = "customer"

    CHOICES = (
        (PER_RESOURCE, "Per resource"),
        (PER_CUSTOMER, "Aggregated per customer"),
    )


class OfferingStates:
    DRAFT = 1
    ACTIVE = 2
    PAUSED = 3
    ARCHIVED = 4
    UNAVAILABLE = 5

    CHOICES = (
        (DRAFT, "Draft"),
        (ACTIVE, "Active"),
        (PAUSED, "Paused"),
        (ARCHIVED, "Archived"),
        (UNAVAILABLE, "Unavailable"),
    )

    ISD_ALLOWED_STATES = (ACTIVE, PAUSED, UNAVAILABLE)

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


class OfferingUserStates:
    # creation flow
    CREATION_REQUESTED = 1
    CREATING = 2
    PENDING_ACCOUNT_LINKING = 3
    PENDING_ADDITIONAL_VALIDATION = 4
    OK = 5
    # removal flow
    DELETION_REQUESTED = 6
    DELETING = 7
    DELETED = 8
    # error states
    ERROR_CREATING = 9
    ERROR_DELETING = 10

    CHOICES = (
        (CREATION_REQUESTED, "Requested"),
        (CREATING, "Creating"),
        (PENDING_ACCOUNT_LINKING, "Pending account linking"),
        (PENDING_ADDITIONAL_VALIDATION, "Pending additional validation"),
        (OK, "OK"),
        (DELETION_REQUESTED, "Requested deletion"),
        (DELETING, "Deleting"),
        (DELETED, "Deleted"),
        (ERROR_CREATING, "Error creating"),
        (ERROR_DELETING, "Error deleting"),
    )

    VALUES = [val for (_, val) in CHOICES]


OfferingUserStatesType = Literal[
    "Requested",
    "Creating",
    "Pending account linking",
    "Pending additional validation",
    "OK",
    "Requested deletion",
    "Deleting",
    "Deleted",
    "Error creating",
    "Error deleting",
]


class OfferingUserRuntimeStates:
    """Runtime/operational state of an offering user account.

    Separate from the lifecycle state, this tracks whether the user can
    actually access the service (e.g. TOU accepted, account linked).
    Can be set freely by the service provider at any lifecycle state
    except Deleted.

    String values are stored in the DB and used directly in the API,
    matching the RuntimeStateMixin pattern used by VMs.
    """

    ACTIVE = "Active"
    PENDING_ACCOUNT_LINKING = "Pending account linking"
    PENDING_ADDITIONAL_VALIDATION = "Pending additional validation"

    CHOICES = (
        (ACTIVE, ACTIVE),
        (PENDING_ACCOUNT_LINKING, PENDING_ACCOUNT_LINKING),
        (PENDING_ADDITIONAL_VALIDATION, PENDING_ADDITIONAL_VALIDATION),
    )

    VALUES = [val for (val, _) in CHOICES]


OfferingUserRuntimeStatesType = Literal[
    "Active",
    "Pending account linking",
    "Pending additional validation",
]


class OrderTypes:
    CREATE = 1
    UPDATE = 2
    TERMINATE = 3
    RESTORE = 4

    CHOICES = (
        (CREATE, "Create"),
        (UPDATE, "Update"),
        (TERMINATE, "Terminate"),
        (RESTORE, "Restore"),
    )

    VALUES = [val for (_, val) in CHOICES]


class CategoryColumnWidget:
    CHOICES = (
        ("csv", "csv"),
        ("filesize", "filesize"),
        ("attached_instance", "attached_instance"),
    )


class OrderStates:
    PENDING_START_DATE = 9
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
        (PENDING_START_DATE, "pending-start-date"),
        (EXECUTING, "executing"),
        (DONE, "done"),
        (ERRED, "erred"),
        (CANCELED, "canceled"),
        (REJECTED, "rejected"),
    )

    TERMINAL_STATES = {DONE, ERRED, CANCELED, REJECTED}
    PENDING_STATES = {
        PENDING_CONSUMER,
        PENDING_PROVIDER,
        PENDING_PROJECT,
        PENDING_START_DATE,
        EXECUTING,
    }
    VALUES = [val for (_, val) in CHOICES]


OrderStatesType = Literal[
    "pending-consumer",
    "pending-provider",
    "pending-project",
    "pending-start-date",
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


class MaintenanceState:
    DRAFT = 1
    SCHEDULED = 2
    IN_PROGRESS = 3
    COMPLETED = 4
    CANCELLED = 5

    CHOICES = (
        (DRAFT, "Draft"),
        (SCHEDULED, "Scheduled"),
        (IN_PROGRESS, "In progress"),
        (COMPLETED, "Completed"),
        (CANCELLED, "Cancelled"),
    )


class MaintenanceType:
    SCHEDULED = 1
    EMERGENCY = 2
    SECURITY = 3
    UPGRADE = 4
    PATCH = 5

    CHOICES = (
        (SCHEDULED, "Scheduled maintenance"),
        (EMERGENCY, "Emergency maintenance"),
        (SECURITY, "Security maintenance"),
        (UPGRADE, "System upgrade"),
        (PATCH, "Patch deployment"),
    )


class ImpactLevel:
    NO_IMPACT = 1
    DEGRADED_PERFORMANCE = 2
    PARTIAL_OUTAGE = 3
    FULL_OUTAGE = 4

    CHOICES = (
        (NO_IMPACT, "No impact"),
        (DEGRADED_PERFORMANCE, "Degraded performance"),
        (PARTIAL_OUTAGE, "Partial outage"),
        (FULL_OUTAGE, "Full outage"),
    )


class MaintenanceTimingBucket:
    """Classification of a maintenance's start/end timing (see
    MaintenanceAnnouncement.timing_bucket)."""

    PENDING = "pending"
    OVERRUN = "overrun"
    LATE_START = "late_start"
    EARLY = "early"
    ON_TIME = "on_time"

    CHOICES = (
        (PENDING, "Pending"),
        (OVERRUN, "Overrun"),
        (LATE_START, "Late start"),
        (EARLY, "Early"),
        (ON_TIME, "On time"),
    )


class RemoteResourceSyncStatus:
    IN_SYNC = "in_sync"
    OUT_OF_SYNC = "out_of_sync"
    SYNC_FAILED = "sync_failed"

    CHOICES = (
        (IN_SYNC, "In sync"),
        (OUT_OF_SYNC, "Out of sync"),
        (SYNC_FAILED, "Sync failed"),
    )


class ServiceAccountState:
    OK = 1
    CLOSED = 2
    ERRED = 3
    CHOICES = (
        (OK, "OK"),
        (CLOSED, "Closed"),
        (ERRED, "Erred"),
    )

    VALUES = [val for (_, val) in CHOICES]


ServiceAccountStatesType = Literal[
    "OK",
    "Closed",
    "Erred",
]


class CourseAccountState(models.IntegerChoices):
    OK = 1, _("OK")
    CLOSED = 2, _("Closed")
    ERRED = 3, _("Erred")
    PENDING = 4, _("Pending")


SUPPORT_OFFERING = "Support.OfferingTemplate"
BOOKING_OFFERING = "Marketplace.Booking"
BASIC_OFFERING = "Marketplace.Basic"
OPENSTACK_TENANT_OFFERING = "OpenStack.Tenant"
OPENSTACK_INSTANCE_OFFERING = "OpenStack.Instance"
OPENSTACK_VOLUME_OFFERING = "OpenStack.Volume"
RANCHER_OFFERING = "Marketplace.Rancher"
VMWARE_VM_OFFERING = "VMware.VirtualMachine"
REMOTE_OFFERING = "Waldur.RemoteOffering"
SCRIPT_OFFERING = "Marketplace.Script"
SLURM_OFFERING = "SlurmInvoices.SlurmPackage"
SITE_AGENT_OFFERING = "Marketplace.Slurm"

# Offering types that can be swapped between each other in-place via the
# `update_type` action. The site-agent processors inherit from the Basic
# processors and only no-op the send paths (delegating to the external
# waldur-site-agent), so the persisted data shape is interchangeable.
SWAPPABLE_OFFERING_TYPES = frozenset({BASIC_OFFERING, SITE_AGENT_OFFERING})


class ResourceAction:
    TERMINATE = "terminate"
    SWITCH_PLAN = "switch_plan"
    UPDATE_LIMITS = "update_limits"
    EDIT_TERMINATION_DATE = "edit_termination_date"
    UPDATE_BACKEND_ID = "update_backend_id"
    UNLINK = "unlink"
    MOVE_RESOURCE = "move_resource"
    SET_SLUG = "set_slug"
    SHOW_USAGE = "show_usage"
    SET_AS_ERRED = "set_as_erred"
    CREATE_ROBOT_ACCOUNT = "create_robot_account"
    SUBMIT_REPORT = "submit_report"
    REPORT_USAGE = "report_usage"
    VIEW_DETAILS = "view_details"
    SYNCHRONIZE = "synchronize"
    VERSION_HISTORY = "version_history"

    CHOICES = (
        (TERMINATE, _("Terminate")),
        (SWITCH_PLAN, _("Switch plan")),
        (UPDATE_LIMITS, _("Update limits")),
        (EDIT_TERMINATION_DATE, _("Edit termination date")),
        (UPDATE_BACKEND_ID, _("Update backend ID")),
        (UNLINK, _("Unlink")),
        (MOVE_RESOURCE, _("Move resource")),
        (SET_SLUG, _("Set slug")),
        (SHOW_USAGE, _("Show usage")),
        (SET_AS_ERRED, _("Set as erred")),
        (CREATE_ROBOT_ACCOUNT, _("Create robot account")),
        (SUBMIT_REPORT, _("Submit report")),
        (REPORT_USAGE, _("Report usage")),
        (VIEW_DETAILS, _("View details")),
        (SYNCHRONIZE, _("Synchronize")),
        (VERSION_HISTORY, _("Version history")),
    )
