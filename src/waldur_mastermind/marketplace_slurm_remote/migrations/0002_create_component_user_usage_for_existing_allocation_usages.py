from django.db import migrations


def create_component_user_usages(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Allocation = apps.get_model("waldur_slurm", "Allocation")
    AllocationUserUsage = apps.get_model("waldur_slurm", "AllocationUserUsage")
    ComponentUserUsage = apps.get_model("marketplace", "ComponentUserUsage")
    ComponentUsage = apps.get_model("marketplace", "ComponentUsage")
    OfferingUser = apps.get_model("marketplace", "OfferingUser")
    Resource = apps.get_model("marketplace", "Resource")
    allocation_ct = ContentType.objects.get_for_model(Allocation)

    for allocation_user_usage in AllocationUserUsage.objects.all():
        allocation = allocation_user_usage.allocation
        username = allocation_user_usage.username
        resource = Resource.objects.filter(
            object_id=allocation.id, content_type=allocation_ct
        ).first()
        if resource is None:
            continue

        offering_user = None
        user = allocation_user_usage.user
        if user is not None:
            offering_user = OfferingUser.objects.filter(
                user=user, offering=resource.offering
            ).first()

        component_usages = ComponentUsage.objects.filter(
            resource=resource,
            billing_period__month=allocation_user_usage.month,
            billing_period__year=allocation_user_usage.year,
        )
        for component_usage in component_usages:
            component = component_usage.component
            usage = getattr(allocation_user_usage, component.type + "_usage", None)
            if usage is None:
                continue

            component_user_usage, _ = ComponentUserUsage.objects.update_or_create(
                username=username,
                component_usage=component_usage,
                defaults={"usage": usage, "user": offering_user},
            )

            print(
                f"ComponentUserUsage {component_user_usage} is set: resource {resource}, component {component}, usage {usage}, offering user {offering_user}"
            )


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace_slurm_remote", "0001_create_offering_users"),
        ("marketplace", "0139_componentuserusage"),
    ]

    operations = [
        migrations.RunPython(create_component_user_usages),
    ]
