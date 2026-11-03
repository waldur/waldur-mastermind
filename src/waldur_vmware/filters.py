from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from rest_framework.filters import BaseFilterBackend

from waldur_core.core import access
from waldur_core.core import filters as core_filters
from waldur_core.core.permissions import has_staff_or_support_read_access
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import (
    filter_queryset_for_user,
    get_connected_customers,
)
from waldur_mastermind.marketplace.models import Offering

from . import models


class UsableSettingsFilterBackend(BaseFilterBackend):
    """Rows of VMware service settings the caller may use: settings behind an active or paused offering the caller may order from, settings of a VM the caller sees, and settings of organizations in which the caller holds an organization-level role. Staff and support see all.

    The inventory views list a settings object's templates, clusters,
    networks, datastores and folders. Consumers read them while ordering
    (the deploy form passes the offering's settings) and while adding a port
    to a VM, so a role filter on the settings' organization would hide them
    from the people who need them.
    """

    # Read by waldur_core.core.access: the first paragraph of the docstring
    # says what lists and retrieves show.
    access_visibility = access.CUSTOM_VISIBILITY

    def filter_queryset(self, request, queryset, view):
        if has_staff_or_support_read_access(request):
            return queryset
        user = request.user
        # Narrowed to offerings of service settings first: the availability
        # rule queries the restricted offerings it may exclude right away.
        offered = (
            Offering.objects.filter(
                content_type=ContentType.objects.get_for_model(
                    structure_models.ServiceSettings
                )
            )
            .filter_by_ordering_availability_for_user(user)
            .values("object_id")
        )
        running = filter_queryset_for_user(
            models.VirtualMachine.objects.all(), user
        ).values("service_settings_id")
        return queryset.filter(
            Q(settings_id__in=offered)
            | Q(settings_id__in=running)
            | Q(settings__customer_id__in=get_connected_customers(user))
        )


class VirtualMachineFilter(structure_filters.BaseResourceFilter):
    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.VirtualMachine
        fields = structure_filters.BaseResourceFilter.Meta.fields + ("runtime_state",)


class PortFilter(structure_filters.BaseResourceFilter):
    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Port

    vm = core_filters.URLFilter(
        view_name="vmware-virtual-machine-detail", field_name="vm__uuid"
    )
    vm_uuid = core_filters.RelatedUUIDFilter(
        view_name="vmware-virtual-machine-detail", field_name="vm__uuid"
    )

    network = core_filters.URLFilter(
        view_name="vmware-network-detail", field_name="network__uuid"
    )
    network_uuid = core_filters.RelatedUUIDFilter(
        view_name="vmware-network-detail", field_name="network__uuid"
    )


class DiskFilter(structure_filters.BaseResourceFilter):
    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Disk

    vm = core_filters.URLFilter(
        view_name="vmware-virtual-machine-detail", field_name="vm__uuid"
    )
    vm_uuid = core_filters.RelatedUUIDFilter(
        view_name="vmware-virtual-machine-detail", field_name="vm__uuid"
    )
    ORDERING_FIELDS = structure_filters.BaseResourceFilter.ORDERING_FIELDS + (
        ("size", "size"),
    )


class TemplateFilter(structure_filters.ServicePropertySettingsFilter):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Template


class ClusterFilter(structure_filters.ServicePropertySettingsFilter):
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", method="filter_customer", label="Customer UUID"
    )

    def filter_customer(self, queryset, name, value):
        return queryset.filter(customercluster__customer__uuid=value)

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Cluster


class NetworkFilter(structure_filters.ServicePropertySettingsFilter):
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", method="filter_customer", label="Customer UUID"
    )
    customer_pair_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        method="filter_customer_pair",
        label="Customer UUID",
    )

    def filter_customer(self, queryset, name, value):
        return queryset.filter(customernetwork__customer__uuid=value)

    def filter_customer_pair(self, queryset, name, value):
        return queryset.filter(customernetworkpair__customer__uuid=value)

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Network


class DatastoreFilter(structure_filters.ServicePropertySettingsFilter):
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", method="filter_customer", label="Customer UUID"
    )

    def filter_customer(self, queryset, name, value):
        return queryset.filter(customerdatastore__customer__uuid=value)

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Datastore


class FolderFilter(structure_filters.ServicePropertySettingsFilter):
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", method="filter_customer", label="Customer UUID"
    )

    def filter_customer(self, queryset, name, value):
        return queryset.filter(customerfolder__customer__uuid=value)

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Folder
