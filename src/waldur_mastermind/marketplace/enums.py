from django.db import models
from django.utils.translation import gettext_lazy as _


class IntegrationStatusStates(models.IntegerChoices):
    """Defines the possible connection states for an integration, such as active or disconnected."""

    UNKNOWN = 1, "Unknown"
    ACTIVE = 2, "Active"
    DISCONNECTED = 3, "Disconnected"


class IntegrationStatusAgentTypes(models.IntegerChoices):
    """Identifies the type of agent reporting the integration status (e.g. order processing, usage reporting)."""

    ORDER_PROCESSING = 1, "Order processing"
    USAGE_REPORTING = 2, "Usage reporting"
    GLAUTH_SYNC = 3, "Glauth sync"
    RESOURCE_SYNC = 4, "Resource sync"
    EVENT_PROCESSING = 5, "Event processing"


class BillingTypes(models.TextChoices):
    """Specifies the billing model for an offering or plan, such as fixed-price or usage-based."""

    # if billing type is fixed, service provider specifies exact values of amount field of plan component model
    FIXED = "fixed", "Fixed-price"
    # if billing type is usage-based billing is applied when usage report is submitted
    USAGE = "usage", "Usage-based"
    # if billing type is limit, user specifies limit when resource is provisioned or updated
    LIMIT = "limit", "Limit-based"
    # if billing type is one-time, billing is applied once on resource activation
    ONE_TIME = "one", "One-time"
    # applies fee on resource activation and every time a plan has changed, using pricing of a new plan
    ON_PLAN_SWITCH = "few", "One-time on plan switch"


class LimitPeriods(models.TextChoices):
    """Defines the period over which usage limits are enforced, such as monthly or annually."""

    MONTH = (
        "month",
        "Maximum monthly - every month service provider can report up to the amount requested by user.",
    )
    QUARTERLY = (
        "quarterly",
        "Maximum quarterly - every quarter service provider can report up to the amount requested by user.",
    )
    ANNUAL = (
        "annual",
        "Maximum annually - every year service provider can report up to the amount requested by user.",
    )
    TOTAL = (
        "total",
        "Maximum total - SP can report up to the requested amount over the whole active state of resource.",
    )


class OfferingStates(models.IntegerChoices):
    """Represents the lifecycle states of a marketplace offering (e.g. Draft, Active, Archived)."""

    DRAFT = 1, "Draft"
    ACTIVE = 2, "Active"
    PAUSED = 3, "Paused"
    ARCHIVED = 4, "Archived"
    UNAVAILABLE = 5, "Unavailable"


class RobotAccountStates(models.IntegerChoices):
    """Tracks the status of a robot account provisioning process."""

    REQUESTED = 1, "Requested"
    CREATING = 2, "Creating"
    OK = 3, "OK"
    REQUESTED_DELETION = 4, "Requested deletion"
    DELETED = 5, "Deleted"
    ERROR = 6, "Error"


class OfferingUserStates(models.IntegerChoices):
    """Tracks the status of an offering user (service provider) account creation or deletion."""

    # creation flow
    CREATION_REQUESTED = 1, "Requested"
    CREATING = 2, "Creating"
    PENDING_ACCOUNT_LINKING = 3, "Pending account linking"
    PENDING_ADDITIONAL_VALIDATION = 4, "Pending additional validation"
    OK = 5, "OK"
    # removal flow
    DELETION_REQUESTED = 6, "Requested deletion"
    DELETING = 7, "Deleting"
    DELETED = 8, "Deleted"
    # error states
    ERROR_CREATING = 9, "Error creating"
    ERROR_DELETING = 10, "Error deleting"


class OrderTypes(models.IntegerChoices):
    """Distinguishes between different types of marketplace orders (Create, Update, Terminate, Restore)."""

    CREATE = 1, "Create"
    UPDATE = 2, "Update"
    TERMINATE = 3, "Terminate"
    RESTORE = 4, "Restore"


class CategoryColumnWidget(models.TextChoices):
    """Specifies the widget type used for rendering category columns in the UI."""

    CSV = "csv", "csv"
    FILESIZE = "filesize", "filesize"
    ATTACHED_INSTANCE = "attached_instance", "attached_instance"


class OrderStates(models.IntegerChoices):
    """Represents the detailed state of an order's processing workflow."""

    PENDING_START_DATE = 9, "pending-start-date"
    PENDING_PROJECT = 8, "pending-project"
    PENDING_CONSUMER = 1, "pending-consumer"
    PENDING_PROVIDER = 7, "pending-provider"
    EXECUTING = 2, "executing"
    DONE = 3, "done"
    ERRED = 4, "erred"
    CANCELED = 5, "canceled"
    REJECTED = 6, "rejected"


ORDER_TERMINAL_STATES = {
    OrderStates.DONE,
    OrderStates.ERRED,
    OrderStates.CANCELED,
    OrderStates.REJECTED,
}

ORDER_PENDING_STATES = {
    OrderStates.PENDING_CONSUMER,
    OrderStates.PENDING_PROVIDER,
    OrderStates.PENDING_PROJECT,
    OrderStates.PENDING_START_DATE,
    OrderStates.EXECUTING,
}


class ResourceStates(models.IntegerChoices):
    """Represents the lifecycle state of a provisioned resource."""

    CREATING = 1, "Creating"
    OK = 2, "OK"
    ERRED = 3, "Erred"
    UPDATING = 4, "Updating"
    TERMINATING = 5, "Terminating"
    TERMINATED = 6, "Terminated"


class MaintenanceState(models.IntegerChoices):
    """Tracks the progress of a maintenance window."""

    DRAFT = 1, "Draft"
    SCHEDULED = 2, "Scheduled"
    IN_PROGRESS = 3, "In progress"
    COMPLETED = 4, "Completed"
    CANCELLED = 5, "Cancelled"


class MaintenanceType(models.IntegerChoices):
    """Categorizes the type of maintenance being performed."""

    SCHEDULED = 1, "Scheduled maintenance"
    EMERGENCY = 2, "Emergency maintenance"
    SECURITY = 3, "Security maintenance"
    UPGRADE = 4, "System upgrade"
    PATCH = 5, "Patch deployment"


class ImpactLevel(models.IntegerChoices):
    """Indicates the expected impact of a maintenance window on service availability."""

    NO_IMPACT = 1, "No impact"
    DEGRADED_PERFORMANCE = 2, "Degraded performance"
    PARTIAL_OUTAGE = 3, "Partial outage"
    FULL_OUTAGE = 4, "Full outage"


class RemoteResourceSyncStatus(models.TextChoices):
    """Indicates the synchronization status of a remote resource."""

    IN_SYNC = "in_sync", "In sync"
    OUT_OF_SYNC = "out_of_sync", "Out of sync"
    SYNC_FAILED = "sync_failed", "Sync failed"


class CatalogType(models.TextChoices):
    """Specifies the type of software catalog."""

    BINARY_RUNTIME = "binary_runtime", _("Binary Runtime (EESSI)")
    SOURCE_PACKAGE = "source_package", _("Source Package (Spack)")
    PACKAGE_MANAGER = "package_manager", _("Package Manager (conda, pip)")


class ServiceAccountState(models.IntegerChoices):
    """Tracks the status of a service account."""

    OK = 1, _("OK")
    CLOSED = 2, _("Closed")
    ERRED = 3, _("Erred")


class CourseAccountState(models.IntegerChoices):
    """Tracks the status of a course account."""

    OK = 1, _("OK")
    CLOSED = 2, _("Closed")
    ERRED = 3, _("Erred")


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


class BackendResourceRequestState(models.TextChoices):
    SENT = "Sent", "Sent"
    PROCESSING = "Processing", "Processing"
    DONE = "Done", "Done"
    ERRED = "Erred", "Erred"
