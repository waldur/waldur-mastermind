from django.db import migrations, models


def migrate_security_groups(apps, schema_editor):
    Instance = apps.get_model("openstack_tenant", "Instance")
    NewSecurityGroup = apps.get_model("openstack", "SecurityGroup")

    for vm in Instance.objects.all():
        for old_security_group in vm.security_groups.all():
            try:
                new_security_group = NewSecurityGroup.objects.get(
                    backend_id=old_security_group.backend_id
                )
                vm.new_security_groups.add(new_security_group)
            except NewSecurityGroup.DoesNotExist:
                print(
                    f"There is no matching SecurityGroup {old_security_group.backend_id}"
                )


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0001_squashed_0028"),
        ("openstack_tenant", "0031_alter_backup_instance_alter_snapshot_source_volume"),
    ]

    operations = [
        migrations.AddField(
            model_name="instance",
            name="new_security_groups",
            field=models.ManyToManyField(to="openstack.securitygroup"),
        ),
        migrations.RunPython(migrate_security_groups),
        migrations.AlterUniqueTogether(
            name="securitygrouprule",
            unique_together=None,
        ),
        migrations.RemoveField(
            model_name="securitygrouprule",
            name="remote_group",
        ),
        migrations.RemoveField(
            model_name="securitygrouprule",
            name="security_group",
        ),
        migrations.RemoveField(
            model_name="instance",
            name="security_groups",
        ),
        migrations.RenameField(
            model_name="instance",
            old_name="new_security_groups",
            new_name="security_groups",
        ),
        migrations.AlterField(
            model_name="instance",
            name="security_groups",
            field=models.ManyToManyField(
                blank=True, related_name="instances", to="openstack.securitygroup"
            ),
        ),
        migrations.DeleteModel(
            name="SecurityGroup",
        ),
        migrations.DeleteModel(
            name="SecurityGroupRule",
        ),
    ]
