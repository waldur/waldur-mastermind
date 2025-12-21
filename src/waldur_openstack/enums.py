from django.db import models


class NetworkShareType(models.TextChoices):
    SHARED = "access_as_shared", "Shared"
    EXTERNAL = "access_as_external", "External"


class InstanceRuntimeStates:
    # All possible OpenStack Instance states on backend.
    # See https://docs.openstack.org/developer/nova/vmstates.html
    ACTIVE = "ACTIVE"
    BUILDING = "BUILDING"
    DELETED = "DELETED"
    SOFT_DELETED = "SOFT_DELETED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"
    HARD_REBOOT = "HARD_REBOOT"
    REBOOT = "REBOOT"
    REBUILD = "REBUILD"
    PASSWORD = "PASSWORD"
    PAUSED = "PAUSED"
    RESCUED = "RESCUED"
    RESIZED = "RESIZED"
    REVERT_RESIZE = "REVERT_RESIZE"
    SHUTOFF = "SHUTOFF"
    STOPPED = "STOPPED"
    SUSPENDED = "SUSPENDED"
    VERIFY_RESIZE = "VERIFY_RESIZE"
