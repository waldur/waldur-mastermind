from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('openstack_tenant', '0018_delete_invalid_security_group_rules'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='securitygrouprule',
            unique_together={('security_group', 'backend_id')},
        ),
    ]
