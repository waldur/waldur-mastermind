from django.db import migrations

from waldur_mastermind.marketplace.enums import BillingTypes

DEPLOYMENT_MODE_SELF_MANAGED = "self_managed"
DEPLOYMENT_MODE_MANAGED = "managed"

# The old, now-removed offering type
OLD_MANAGED_RANCHER_OFFERING_TYPE = "Marketplace.ManagedRancher"

# The unified offering type
RANCHER_OFFERING_TYPE = "Marketplace.Rancher"

# New components for usage-based billing, as seen in the diff
RANCHER_BILLING_COMPONENTS = [
    {
        "type": "cpu_hours",
        "name": "CPU hours",
        "measured_unit": "vCPU-hours",
    },
    {
        "type": "ram_hours",
        "name": "RAM hours",
        "measured_unit": "GB-hours",
    },
    {
        "type": "storage_hours",
        "name": "Storage hours",
        "measured_unit": "GB-hours",
    },
]


def unify_and_migrate_rancher_plugins(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")
    Resource = apps.get_model("marketplace", "Resource")
    OfferingComponent = apps.get_model("marketplace", "OfferingComponent")
    InvoiceItem = apps.get_model("invoices", "InvoiceItem")
    Cluster = apps.get_model("waldur_rancher", "Cluster")

    # STEP 1: Update existing "self-managed" Rancher offerings
    # This ensures that all pre-existing standard Rancher offerings are compatible with the new logic.
    for offering in Offering.objects.filter(type=RANCHER_OFFERING_TYPE):
        if "deployment_mode" not in offering.plugin_options:
            offering.plugin_options["deployment_mode"] = DEPLOYMENT_MODE_SELF_MANAGED
            offering.save(update_fields=["plugin_options"])

    # STEP 2: Find and migrate old "ManagedRancher" offerings
    managed_offerings = Offering.objects.filter(type=OLD_MANAGED_RANCHER_OFFERING_TYPE)
    if not managed_offerings.exists():
        # If there are no old managed offerings, the migration is complete.
        return

    private_offerings_to_delete_ids = set()

    for offering in managed_offerings:
        # The scope of a ManagedRancher offering was another "private" Rancher offering.
        private_rancher_offering = offering.scope
        if not private_rancher_offering:
            continue

        private_offerings_to_delete_ids.add(private_rancher_offering.id)

        # Repoint the scope to the underlying ServiceSettings
        service_settings = private_rancher_offering.scope
        if not service_settings:
            continue

        # Update the offering to the new unified format
        offering.type = RANCHER_OFFERING_TYPE
        offering.plugin_options["deployment_mode"] = DEPLOYMENT_MODE_MANAGED
        offering.scope = service_settings
        offering.save()

        # Create new usage-based components for the migrated offering
        for component_data in RANCHER_BILLING_COMPONENTS:
            OfferingComponent.objects.update_or_create(
                offering=offering,
                type=component_data["type"],
                defaults={
                    "name": component_data["name"],
                    "measured_unit": component_data["measured_unit"],
                    "billing_type": BillingTypes.USAGE,
                },
            )

    # STEP 3: Flatten the resource structure for migrated offerings
    managed_resources = Resource.objects.filter(offering__in=managed_offerings)
    nested_resources_to_delete_ids = set()

    for resource in managed_resources:
        # The scope of a ManagedRancher resource was another Resource.
        nested_resource = resource.scope
        if not nested_resource or not isinstance(nested_resource, Resource):
            continue

        # The scope of the nested resource was the actual Cluster object.
        cluster = nested_resource.scope
        if not cluster or not isinstance(cluster, Cluster):
            continue

        nested_resources_to_delete_ids.add(nested_resource.id)

        # Repoint the main resource's scope directly to the cluster
        resource.scope = cluster
        resource.save()

    # STEP 4: Clean up old invoice items for migrated resources
    # Since the billing model changes from invoice-copying to usage-reporting,
    # old invoice items for these resources become obsolete and could cause double-billing.
    InvoiceItem.objects.filter(resource__in=managed_resources).delete()

    # STEP 5: Clean up orphaned intermediate resources and offerings
    if nested_resources_to_delete_ids:
        Resource.objects.filter(id__in=nested_resources_to_delete_ids).delete()

    if private_offerings_to_delete_ids:
        Offering.objects.filter(id__in=private_offerings_to_delete_ids).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace_rancher", "0003_usage"),
        (
            "waldur_rancher",
            "0052_alter_clustersecuritygrouprule_backend_id_and_more",
        ),
        ("marketplace", "0181_alter_offeringtermsofservice_is_active_and_more"),
    ]

    operations = [
        migrations.RunPython(
            unify_and_migrate_rancher_plugins, reverse_code=migrations.RunPython.noop
        ),
    ]
