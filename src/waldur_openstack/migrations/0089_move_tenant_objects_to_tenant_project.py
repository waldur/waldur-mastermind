from collections import defaultdict

from django.db import migrations
from django.db.models import F

# As in handlers.TENANT_OBJECTS: a tenant's own objects, with the path from
# each to its tenant.
TENANT_OBJECTS = (
    ("Network", "tenant"),
    ("SubNet", "tenant"),
    ("Port", "tenant"),
    ("Router", "tenant"),
    ("FloatingIP", "tenant"),
    ("SecurityGroup", "tenant"),
    ("ServerGroup", "tenant"),
    ("LoadBalancer", "tenant"),
    ("Pool", "load_balancer__tenant"),
    ("Listener", "load_balancer__tenant"),
    ("PoolMember", "pool__load_balancer__tenant"),
    ("HealthMonitor", "pool__load_balancer__tenant"),
)


def move_tenant_objects_to_tenant_project(apps, schema_editor):
    """Tenants moved before their objects went with them left those behind."""
    for model_name, tenant_path in TENANT_OBJECTS:
        model = apps.get_model("openstack", model_name)
        tenant_project = f"{tenant_path}__project_id"
        stale = (
            model.objects.exclude(**{f"{tenant_project}__isnull": True})
            .exclude(project_id=F(tenant_project))
            .values_list("pk", tenant_project)
        )
        by_project = defaultdict(list)
        for pk, project_id in stale:
            by_project[project_id].append(pk)
        for project_id, pks in by_project.items():
            model.objects.filter(pk__in=pks).update(project_id=project_id)


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0088_tenant_is_managed"),
    ]

    operations = [
        migrations.RunPython(
            move_tenant_objects_to_tenant_project, migrations.RunPython.noop
        ),
    ]
