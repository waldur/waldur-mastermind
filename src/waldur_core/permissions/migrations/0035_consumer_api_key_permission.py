from django.db import migrations

# A resource's API keys were governed by RESOURCE.MANAGE_USERS; they now have a
# permission of their own. Every role that held the old one keeps managing keys
# with the new one, including roles staff created by hand, and so does every
# personal access token scoped to it: a token's scopes are permission names,
# and a token cannot be edited, only minted again.
#
# The rest mirrors docker/rootfs/etc/waldur/permissions.yaml, which grants the new
# permission to project managers and administrators as well. Deployments get the
# system roles' grants from import_roles, but clones (PROJECT.<slug>.MANAGER, ...)
# copied their template's permissions once and never see later additions, and
# stacks that skip initdb never run import_roles at all.
OLD = "RESOURCE.MANAGE_USERS"
NEW = "RESOURCE.MANAGE_CONSUMER_API_KEY"
TEMPLATES = {
    "customer": ["CUSTOMER.OWNER"],
    "project": ["PROJECT.ADMIN", "PROJECT.MANAGER"],
}


def add_consumer_api_key_permission(apps, schema_editor):
    Role = apps.get_model("permissions", "Role")
    RolePermission = apps.get_model("permissions", "RolePermission")
    role_ids = set(
        RolePermission.objects.filter(permission=OLD).values_list("role_id", flat=True)
    )
    for scope, template_names in TEMPLATES.items():
        roles = Role.objects.filter(
            content_type__model=scope, is_system_role=True, name__in=template_names
        )
        clones = Role.objects.filter(
            content_type__model=scope,
            template__name__in=template_names,
            template__is_system_role=True,
        )
        role_ids.update(roles.values_list("id", flat=True))
        role_ids.update(clones.values_list("id", flat=True))
    for role_id in role_ids:
        RolePermission.objects.get_or_create(role_id=role_id, permission=NEW)

    PersonalAccessToken = apps.get_model("core", "PersonalAccessToken")
    tokens = PersonalAccessToken.objects.filter(scopes__contains=[OLD]).exclude(
        scopes__contains=[NEW]
    )
    for token in tokens:
        token.scopes = [*token.scopes, NEW]
        token.save(update_fields=["scopes"])


class Migration(migrations.Migration):
    dependencies = [
        ("permissions", "0034_role_description_en_gb"),
        ("core", "0029_personal_access_token"),
    ]

    operations = [
        migrations.RunPython(add_consumer_api_key_permission, migrations.RunPython.noop)
    ]
