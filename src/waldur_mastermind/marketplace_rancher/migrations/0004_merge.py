from django.db import migrations

from waldur_mastermind.marketplace.enums import BillingTypes

DEPLOYMENT_MODE_SELF_MANAGED = "self_managed"
DEPLOYMENT_MODE_MANAGED = "managed"

# The old, now-removed offering type
OLD_MANAGED_RANCHER_OFFERING_TYPE = "Marketplace.ManagedRancher"

# The unified offering type
RANCHER_OFFERING_TYPE = "Marketplace.Rancher"

# New components for usage-based billing
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
    ContentType = apps.get_model("contenttypes", "ContentType")

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

    offering_content_type = ContentType.objects.get_for_model(Offering)
    private_offerings_to_delete_ids = set()

    for offering in managed_offerings:
        # The scope of a ManagedRancher offering was another "private" Rancher offering.
        # We need to find this private offering and get ITS scope (the ServiceSettings).

        # Check if the scope is indeed another Offering
        if offering.content_type_id != offering_content_type.id:
            continue

        try:
            private_rancher_offering = Offering.objects.get(id=offering.object_id)
        except Offering.DoesNotExist:
            continue

        private_offerings_to_delete_ids.add(private_rancher_offering.id)

        # The scope of the private offering is the actual ServiceSettings.
        # We repoint the managed offering directly to it.
        new_content_type = private_rancher_offering.content_type
        new_object_id = private_rancher_offering.object_id

        if not new_content_type or not new_object_id:
            continue

        # Update the offering to the new unified format
        offering.type = RANCHER_OFFERING_TYPE
        offering.plugin_options["deployment_mode"] = DEPLOYMENT_MODE_MANAGED
        offering.content_type = new_content_type
        offering.object_id = new_object_id
        offering.save(
            update_fields=["type", "plugin_options", "content_type_id", "object_id"]
        )

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
    migrated_offering_ids = [o.id for o in managed_offerings]
    managed_resources = Resource.objects.filter(offering_id__in=migrated_offering_ids)

    # Get ContentTypes for Resource and Cluster models
    resource_content_type = ContentType.objects.get_for_model(Resource)
    cluster_content_type = ContentType.objects.get_for_model(Cluster)

    for resource in managed_resources:
        # The scope of a ManagedRancher resource was another Resource.
        # We need to find this nested resource and get ITS scope (the Cluster).

        # Check if the resource's scope is another resource
        if resource.content_type_id != resource_content_type.id:
            continue

        try:
            nested_resource = Resource.objects.get(id=resource.object_id)
        except Resource.DoesNotExist:
            continue

        # Check if the nested resource's scope is a Cluster
        if nested_resource.content_type_id != cluster_content_type.id:
            continue

        # 1. Store the target scope information from the nested resource.
        new_content_type_id = nested_resource.content_type_id
        new_object_id = nested_resource.object_id

        # 2. Delete the nested resource to free up the unique constraint.
        nested_resource.delete()

        # 3. Now, update the main resource to point directly to the cluster.
        resource.content_type_id = new_content_type_id
        resource.object_id = new_object_id
        resource.save(update_fields=["content_type_id", "object_id"])

    # STEP 4: Clean up old invoice items for migrated resources
    # Since the billing model changes, old invoice items become obsolete.
    InvoiceItem.objects.filter(resource__in=managed_resources).delete()

    # STEP 5: Clean up orphaned intermediate offerings
    # The nested resources are now deleted within the loop in STEP 3.
    if private_offerings_to_delete_ids:
        Offering.objects.filter(id__in=private_offerings_to_delete_ids).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("contenttypes", "__latest__"),
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
