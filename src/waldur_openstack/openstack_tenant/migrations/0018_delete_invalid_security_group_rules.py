from django.db import migrations


def delete_invalid_security_group_rules(apps, schema_editor):
    SecurityGroupRuleProperty = apps.get_model('openstack_tenant', 'SecurityGroupRule')
    SecurityGroupRuleProperty.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('openstack_tenant', '0017_pull_remote_group'),
    ]

    operations = [
        migrations.RunPython(delete_invalid_security_group_rules),
    ]
