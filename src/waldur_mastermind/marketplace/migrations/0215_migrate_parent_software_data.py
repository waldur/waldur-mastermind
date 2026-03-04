from django.db import migrations


def copy_parent_software_to_m2m(apps, schema_editor):
    """Copy FK parent_software values to M2M parent_softwares."""
    SoftwarePackage = apps.get_model("marketplace", "SoftwarePackage")
    for package in SoftwarePackage.objects.filter(
        parent_software__isnull=False
    ).select_related("parent_software"):
        package.parent_softwares.add(package.parent_software)


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0214_softwarepackage_parent_softwares_m2m"),
    ]

    operations = [
        migrations.RunPython(
            copy_parent_software_to_m2m,
            migrations.RunPython.noop,
        ),
        migrations.RemoveField(
            model_name="softwarepackage",
            name="parent_software",
        ),
    ]
