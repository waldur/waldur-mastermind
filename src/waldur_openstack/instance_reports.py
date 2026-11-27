"""OpenStack instance reporting queries and filters for marketplace stats."""

from typing import Any

import django_filters
from django.db.models import Count, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce

from waldur_core.core import filters as core_filters
from waldur_core.core.enums import CoreStates
from waldur_openstack import models as openstack_models


class OpenStackInstanceReportFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="icontains")
    flavor_name = django_filters.CharFilter(lookup_expr="icontains")
    image_name = django_filters.CharFilter(lookup_expr="icontains")
    hypervisor_hostname = django_filters.CharFilter(lookup_expr="icontains")

    runtime_state = django_filters.CharFilter()
    availability_zone_name = django_filters.CharFilter(
        field_name="availability_zone__name",
    )

    cores_min = django_filters.NumberFilter(field_name="cores", lookup_expr="gte")
    cores_max = django_filters.NumberFilter(field_name="cores", lookup_expr="lte")
    ram_min = django_filters.NumberFilter(field_name="ram", lookup_expr="gte")
    ram_max = django_filters.NumberFilter(field_name="ram", lookup_expr="lte")
    disk_min = django_filters.NumberFilter(field_name="disk", lookup_expr="gte")
    disk_max = django_filters.NumberFilter(field_name="disk", lookup_expr="lte")

    service_settings_uuid = django_filters.UUIDFilter(
        field_name="service_settings__uuid",
    )
    customer_uuid = django_filters.UUIDFilter(
        field_name="project__customer__uuid",
    )
    project_uuid = django_filters.UUIDFilter(
        field_name="project__uuid",
    )
    tenant_uuid = django_filters.UUIDFilter(
        field_name="tenant__uuid",
    )

    state = core_filters.MappedMultipleChoiceFilter(
        choices=CoreStates.choices,
    )

    o = django_filters.OrderingFilter(
        fields=(
            ("name", "name"),
            ("cores", "cores"),
            ("ram", "ram"),
            ("disk", "disk"),
            ("created", "created"),
            ("runtime_state", "runtime_state"),
            ("flavor_name", "flavor_name"),
            ("hypervisor_hostname", "hypervisor_hostname"),
            ("project__customer__name", "customer_name"),
            ("project__name", "project_name"),
            ("service_settings__name", "cluster_name"),
            ("start_time", "start_time"),
        ),
    )

    class Meta:
        model = openstack_models.Instance
        fields = []


def get_instance_report_queryset(request):
    queryset = (
        openstack_models.Instance.objects.all()
        .select_related(
            "service_settings",
            "project__customer",
            "tenant",
            "availability_zone",
        )
        .prefetch_related("ports__floating_ips", "volumes")
    )

    filterset = OpenStackInstanceReportFilter(request.query_params, queryset=queryset)
    return filterset.qs


def build_instance_report_rows(instances) -> list[dict[str, Any]]:
    data = []
    for instance in instances:
        volumes = instance.volumes.all()
        ports = instance.ports.all()
        floating_ips = []
        internal_ips = set()
        for port in ports:
            for fip in port.floating_ips.all():
                floating_ips.append(fip)
            if port.fixed_ips:
                for fixed_ip in port.fixed_ips:
                    ip_address = (
                        fixed_ip.get("ip_address")
                        if isinstance(fixed_ip, dict)
                        else None
                    )
                    if ip_address:
                        internal_ips.add(ip_address)

        data.append(
            {
                "uuid": instance.uuid,
                "name": instance.name,
                "created": instance.created,
                "cores": instance.cores,
                "ram": instance.ram,
                "disk": instance.disk,
                "flavor_name": instance.flavor_name,
                "flavor_disk": instance.flavor_disk,
                "image_name": instance.image_name,
                "hypervisor_hostname": instance.hypervisor_hostname,
                "runtime_state": instance.runtime_state,
                "state": instance.get_state_display(),
                "availability_zone_name": (
                    instance.availability_zone.name
                    if instance.availability_zone
                    else None
                ),
                "start_time": instance.start_time,
                "service_settings_uuid": instance.service_settings.uuid,
                "service_settings_name": instance.service_settings.name,
                "tenant_uuid": instance.tenant.uuid if instance.tenant else None,
                "tenant_name": instance.tenant.name if instance.tenant else "",
                "project_uuid": instance.project.uuid,
                "project_name": instance.project.name,
                "customer_uuid": instance.project.customer.uuid,
                "customer_name": instance.project.customer.name,
                "customer_abbreviation": instance.project.customer.abbreviation,
                "volume_count": len(volumes),
                "total_volume_size_mb": sum(v.size for v in volumes),
                "floating_ip_count": len(floating_ips),
                "port_count": len(ports),
                "internal_ips": sorted(internal_ips),
                "external_ips": sorted(
                    {fip.address for fip in floating_ips if fip.address}
                ),
            }
        )
    return data


def aggregate_instance_report(request, mapping: dict[str, Any]) -> list[dict[str, Any]]:
    key_field = mapping["key_field"]
    label_field = mapping.get("label_field")

    queryset = openstack_models.Instance.objects.all()
    filterset = OpenStackInstanceReportFilter(request.query_params, queryset=queryset)
    queryset = filterset.qs

    group_fields = [key_field]
    if label_field:
        group_fields.append(label_field)

    # Use per-instance subqueries for volume size and floating IP count,
    # then aggregate the subquery results per group. This avoids both
    # cross-join inflation and the N+1 of per-group queries.
    volume_size_subquery = (
        openstack_models.Volume.objects.filter(instance=OuterRef("pk"))
        .order_by()
        .values("instance")
        .annotate(total=Sum("size"))
        .values("total")
    )
    floating_ip_subquery = (
        openstack_models.FloatingIP.objects.filter(port__instance=OuterRef("pk"))
        .order_by()
        .values("port__instance")
        .annotate(total=Count("id"))
        .values("total")
    )

    annotated_qs = queryset.annotate(
        _vol_size=Coalesce(Subquery(volume_size_subquery), 0),
        _fip_count=Coalesce(Subquery(floating_ip_subquery), 0),
    )

    aggregated = (
        annotated_qs.values(*group_fields)
        .annotate(
            instance_count=Count("id"),
            total_cores=Sum("cores"),
            total_ram_mb=Sum("ram"),
            total_disk_mb=Sum("disk"),
            total_volume_size_mb=Sum("_vol_size"),
            total_floating_ips=Sum("_fip_count"),
        )
        .order_by("-instance_count")
    )

    data = []
    for row in aggregated:
        key = row[key_field]
        label = row.get(label_field, key) if label_field else key
        data.append(
            {
                "group_key": str(key) if key is not None else "",
                "group_label": str(label) if label is not None else "",
                "instance_count": row["instance_count"],
                "total_cores": row["total_cores"],
                "total_ram_mb": row["total_ram_mb"],
                "total_disk_mb": row["total_disk_mb"],
                "total_volume_size_mb": row["total_volume_size_mb"],
                "total_floating_ips": row["total_floating_ips"],
            }
        )
    return data
