from django.db import migrations
from django.db.models import Count


def delete_port_duplicates(apps, schema_editor):
    Port = apps.get_model("openstack", "Port")

    duplicates = (
        Port.objects.values("tenant", "backend_id")
        .annotate(count=Count("id"))
        .filter(count__gt=1)
    )

    for duplicate in duplicates:
        ports = Port.objects.filter(
            tenant=duplicate["tenant"], backend_id=duplicate["backend_id"]
        )
        # Keep the first one, delete the rest
        ports.exclude(id=ports.first().id).delete()


def delete_router_duplicates(apps, schema_editor):
    Router = apps.get_model("openstack", "Router")

    duplicates = (
        Router.objects.values("tenant", "backend_id")
        .annotate(count=Count("id"))
        .filter(count__gt=1)
    )

    for duplicate in duplicates:
        routers = Router.objects.filter(
            tenant=duplicate["tenant"], backend_id=duplicate["backend_id"]
        )
        # Keep the first one, delete the rest
        routers.exclude(id=routers.first().id).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0052_router_ports"),
    ]

    operations = [
        migrations.RunPython(delete_port_duplicates),
        migrations.RunPython(delete_router_duplicates),
    ]
