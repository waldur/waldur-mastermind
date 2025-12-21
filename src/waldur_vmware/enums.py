from django.db import models
from django.utils.translation import gettext_lazy as _


class VirtualMachineRuntimeStates(models.TextChoices):
    POWERED_OFF = "POWERED_OFF", _("Powered off")
    POWERED_ON = "POWERED_ON", _("Powered on")
    SUSPENDED = "SUSPENDED", _("Suspended")


class VirtualMachineGuestPowerStates(models.TextChoices):
    RUNNING = "RUNNING", _("Running")
    SHUTTING_DOWN = "SHUTTING_DOWN", _("Shutting down")
    RESETTING = "RESETTING", _("Resetting")
    STANDBY = "STANDBY", _("Standby")
    NOT_RUNNING = "NOT_RUNNING", _("Not running")
    UNAVAILABLE = "UNAVAILABLE", _("Unavailable")


class VirtualMachineToolsStates(models.TextChoices):
    STARTING = "STARTING", _("Starting")
    RUNNING = "RUNNING", _("Running")
    NOT_RUNNING = "NOT_RUNNING", _("Not running")
