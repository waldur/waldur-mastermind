from django.db import migrations

# Mirrors docker/rootfs/etc/waldur/permissions.yaml, which grants
# OFFERING.UPDATE_AGENT_CONFIGURATION to every role that has
# OFFERING.UPDATE_INTEGRATION (organization owner, service provider manager,
# offering manager). Changing where a site agent connects or what it runs used
# to need only OFFERING.UPDATE_INTEGRATION and now needs both. import_roles fixes
# the system roles on the next deploy, but clones (CUSTOMER.<slug>.OWNER) copied
# their template's permissions once and never see later additions, a
# deployment's own roles are not in permissions.yaml at all, and stacks that
# skip initdb never run import_roles. So every role that may change an
# offering's integration today keeps changing all of its agent configuration;
# a deployment narrows that with permissions-override.yaml.
#
# Personal access tokens are not given the permission: a token changes those
# settings only when it is issued with the permission among its scopes.
PERMISSION = "OFFERING.UPDATE_AGENT_CONFIGURATION"
HELD_WITH = "OFFERING.UPDATE_INTEGRATION"


def grant_agent_configuration(apps, schema_editor):
    Role = apps.get_model("permissions", "Role")
    RolePermission = apps.get_model("permissions", "RolePermission")
    for role in Role.objects.filter(permissions__permission=HELD_WITH).distinct():
        RolePermission.objects.get_or_create(role=role, permission=PERMISSION)


class Migration(migrations.Migration):
    dependencies = [
        ("permissions", "0039_reveal_api_key_permission"),
    ]

    operations = [
        migrations.RunPython(grant_agent_configuration, migrations.RunPython.noop)
    ]
