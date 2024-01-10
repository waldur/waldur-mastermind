from django.db import models
from django.utils.translation import gettext_lazy as _

from waldur_core.structure import models as structure_models


class Region(structure_models.GeneralServiceProperty):
    @classmethod
    def get_url_name(cls):
        return "digitalocean-region"


class Image(structure_models.GeneralServiceProperty):
    regions = models.ManyToManyField(Region)
    distribution = models.CharField(max_length=100)
    type = models.CharField(max_length=100)
    is_official = models.BooleanField(
        default=False, help_text=_("Is image provided by DigitalOcean")
    )
    min_disk_size = models.PositiveIntegerField(
        null=True, help_text=_("Minimum disk required for a size to use this image")
    )
    created_at = models.DateTimeField(null=True)

    @property
    def is_ssh_key_mandatory(self):
        MANDATORY = "Ubuntu", "FreeBSD", "CoreOS"
        return self.distribution in MANDATORY

    def __str__(self):
        return f"{self.name} {self.distribution} ({self.type})"

    @classmethod
    def get_url_name(cls):
        return "digitalocean-image"

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "type",
            "distribution",
            "is_official",
            "min_disk_size",
            "created_at",
        )


class Size(structure_models.GeneralServiceProperty):
    regions = models.ManyToManyField(Region)

    cores = models.PositiveSmallIntegerField(help_text=_("Number of cores in a VM"))
    ram = models.PositiveIntegerField(help_text=_("Memory size in MiB"))
    disk = models.PositiveIntegerField(help_text=_("Disk size in MiB"))
    transfer = models.PositiveIntegerField(
        help_text=_("Amount of transfer bandwidth in MiB")
    )
    price = models.DecimalField(
        _("Hourly price rate"), default=0, max_digits=11, decimal_places=5
    )

    @classmethod
    def get_url_name(cls):
        return "digitalocean-size"

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "cores",
            "ram",
            "disk",
            "transfer",
            "price",
        )


class Droplet(structure_models.VirtualMachine):
    transfer = models.PositiveIntegerField(
        default=0, help_text=_("Amount of transfer bandwidth in MiB")
    )
    ip_address = models.GenericIPAddressField(null=True, protocol="IPv4", blank=True)
    region_name = models.CharField(max_length=150, blank=True)
    size_name = models.CharField(max_length=150, blank=True)

    @property
    def external_ips(self):
        return [self.ip_address]

    @property
    def internal_ips(self):
        return []

    @classmethod
    def get_url_name(cls):
        return "digitalocean-droplet"

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "state",
            "runtime_state",
            "image_name",
        )
