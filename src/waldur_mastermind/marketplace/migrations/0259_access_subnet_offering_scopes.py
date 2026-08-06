import logging

import django.db.models.deletion
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.logging.mixins
from waldur_mastermind.marketplace.migrations._collapse_resource_subnets import (
    plan_collapse,
)

logger = logging.getLogger(__name__)

# Resource.States.TERMINATED. Spelled out rather than imported: a migration must
# keep working when the enum on the live model changes.
RESOURCE_STATE_TERMINATED = 6


def collapse_resource_subnets(apps, schema_editor):
    """Move per-resource access subnets onto the organization, scoped by offering.

    Consumer subnets used to be per resource. They now live as one entry per
    (organization, address) on structure.AccessSubnet, with this table recording
    which offerings each entry applies to.

    Collapsing per (customer, offering) is a union, so a resource whose list was
    narrower than a sibling's becomes reachable from the addresses that sibling
    allowed. That widening cannot be avoided while preserving everyone's
    existing access, so it is reported instead — these log lines are the
    permanent record of which resources gained which addresses.

    Migrated entries get applies_to_portal=False: they governed reaching a
    backend, never signing in, and turning them into sign-in restrictions would
    lock organizations out of the portal.
    """
    ResourceAccessSubnet = apps.get_model("marketplace", "ResourceAccessSubnet")
    AccessSubnetOfferingScope = apps.get_model(
        "marketplace", "AccessSubnetOfferingScope"
    )
    AccessSubnet = apps.get_model("structure", "AccessSubnet")
    Resource = apps.get_model("marketplace", "Resource")

    resources = (
        Resource.objects.filter(
            offering__in=ResourceAccessSubnet.objects.values("resource__offering")
        )
        .exclude(state=RESOURCE_STATE_TERMINATED)
        .select_related("offering", "project__customer")
    )
    subnets_by_resource: dict[int, list] = {}
    for subnet in ResourceAccessSubnet.objects.exclude(inet__isnull=True):
        subnets_by_resource.setdefault(subnet.resource_id, []).append(subnet)

    def rows():
        for resource in resources:
            base = {
                "customer_id": resource.project.customer_id,
                "customer_name": resource.project.customer.name,
                "offering_id": resource.offering_id,
                "offering_name": resource.offering.name,
                "resource_name": resource.name,
            }
            own = subnets_by_resource.get(resource.id)
            if not own:
                yield {**base, "inet": None, "description": ""}
                continue
            for subnet in own:
                yield {
                    **base,
                    "inet": str(subnet.inet),
                    "description": subnet.description or "",
                }

    scopes = 0
    plan = plan_collapse(rows())
    for pair in plan:
        for item in pair["widened"]:
            logger.warning(
                "Access subnet widening: resource '%s' (%s / %s) gains %s from "
                "the collapsed organization list.",
                item["resource_name"],
                pair["customer_name"],
                pair["offering_name"],
                ", ".join(item["gained"]),
            )
        for resource_name in pair["newly_restricted"]:
            logger.warning(
                "Access subnet concealment change: resource '%s' (%s / %s) had no "
                "subnets and now inherits %s.",
                resource_name,
                pair["customer_name"],
                pair["offering_name"],
                ", ".join(pair["union"]),
            )
        for inet, description in pair["inets"].items():
            # An address the organization already trusts keeps its existing row —
            # including its portal scope — and simply gains this offering.
            subnet, created = AccessSubnet.objects.get_or_create(
                customer_id=pair["customer_id"],
                inet=inet,
                defaults={
                    "description": description,
                    "applies_to_portal": False,
                    "is_staff_managed": False,
                },
            )
            AccessSubnetOfferingScope.objects.get_or_create(
                access_subnet=subnet, offering_id=pair["offering_id"]
            )
            scopes += 1

    if scopes:
        logger.info(
            "Moved per-resource access subnets onto %s organization entries "
            "across %s (organization, offering) pairs.",
            scopes,
            len(plan),
        )


def restore_resource_subnets(apps, schema_editor):
    """Fan the organization list back out to every resource of each scoped offering.

    The original per-resource split is not recoverable — it was unioned away —
    so every live resource of the offering gets the whole list. The reverse is
    therefore lossy in the permissive direction, which is the only direction
    that keeps existing access working.
    """
    ResourceAccessSubnet = apps.get_model("marketplace", "ResourceAccessSubnet")
    AccessSubnetOfferingScope = apps.get_model(
        "marketplace", "AccessSubnetOfferingScope"
    )
    Resource = apps.get_model("marketplace", "Resource")

    for scope in AccessSubnetOfferingScope.objects.select_related(
        "access_subnet"
    ).exclude(access_subnet__inet__isnull=True):
        resources = Resource.objects.filter(
            project__customer_id=scope.access_subnet.customer_id,
            offering_id=scope.offering_id,
        ).exclude(state=RESOURCE_STATE_TERMINATED)
        for resource in resources:
            ResourceAccessSubnet.objects.update_or_create(
                resource=resource,
                inet=scope.access_subnet.inet,
                defaults={"description": scope.access_subnet.description},
            )


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0258_derive_service_access_mode"),
        ("structure", "0079_accesssubnet_scopes"),
    ]

    operations = [
        migrations.CreateModel(
            name="AccessSubnetOfferingScope",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "access_subnet",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="structure.accesssubnet",
                    ),
                ),
                (
                    "offering",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="access_subnet_scopes",
                        to="marketplace.offering",
                    ),
                ),
            ],
            options={
                "ordering": ["offering__name"],
                "unique_together": {("access_subnet", "offering")},
            },
            bases=(models.Model, waldur_core.logging.mixins.LoggableMixin),
        ),
        migrations.RunPython(
            collapse_resource_subnets,
            restore_resource_subnets,
        ),
        migrations.DeleteModel(
            name="ResourceAccessSubnet",
        ),
    ]
