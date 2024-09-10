import django.db.models.deletion
from django.db import migrations, models


def migrate_image(apps, schema_editor):
    Volume = apps.get_model("openstack_tenant", "Volume")
    Image = apps.get_model("openstack", "Image")
    Tenant = apps.get_model("openstack", "Tenant")

    for volume in Volume.objects.exclude(image__isnull=True).all():
        tenant_id = volume.service_settings.object_id
        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            print(f"Tenant {tenant_id} is not found")
            continue
        try:
            volume.new_image = Image.objects.get(
                backend_id=volume.image.backend_id,
                settings=tenant.service_settings,
            )
            volume.save(update_fields=["new_image"])
        except Image.DoesNotExist:
            print(f"There is no matching image {volume.image.backend_id}")


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0034_image_tenant"),
        ("openstack_tenant", "0037_merge_flavor"),
    ]

    operations = [
        migrations.AddField(
            model_name="volume",
            name="new_image",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.SET_NULL,
                to="openstack.image",
                blank=True,
                null=True,
            ),
        ),
        migrations.RunPython(migrate_image),
        migrations.RemoveField(
            model_name="volume",
            name="image",
        ),
        migrations.RenameField(
            model_name="volume",
            old_name="new_image",
            new_name="image",
        ),
        migrations.AlterField(
            model_name="volume",
            name="image",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="openstack.image",
            ),
        ),
        migrations.DeleteModel(
            name="Image",
        ),
    ]
