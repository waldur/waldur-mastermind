import enums


def format_enum(enum):
    return "\n".join(
        f"  {key}: '{value.value}'," for key, value in enum._member_map_.items()
    )


# Keys exposed to the frontend must match the public TYPE_MAP / SDK RoleType
# convention (e.g. ``resource_project``, ``call_organizer``), not Django's
# model_name (``resourceproject``, ``callmanagingorganisation``). The
# Python-side dict still uses model_name because that's what callers look up
# via ``model_class._meta.model_name``. A service provider is keyed both ways
# there (``service_provider`` and ``serviceprovider``, with the same
# permission), so its model-name key folds into the alias here.
_KEY_REMAP = {
    "resourceproject": "resource_project",
    "callmanagingorganisation": "call_organizer",
    "serviceprovider": "service_provider",
}


# A site-wide role has no create permission: only staff grant it, through
# /api/global-user-roles/. The frontend keys this map by every SDK RoleType,
# so the site scope is listed too, mapped to STAFF.ACCESS. No role carries that
# permission, so the frontend's check passes for staff alone.
_EXTRA_SCOPES = {"global": enums.PermissionEnum.STAFF_ACCESS}


# What a site-wide role cannot carry. The role form greys these out instead of
# offering a choice the API rejects.
def format_list(permissions):
    return "\n".join(f"  '{permission.value}'," for permission in sorted(permissions))


def format_dict(enum):
    remapped = {}
    for key, value in {**enum, **_EXTRA_SCOPES}.items():
        key = _KEY_REMAP.get(key, key)
        if remapped.setdefault(key, value) != value:
            raise ValueError(f"Keys mapped to {key!r} carry different permissions.")
    return "\n".join(f"  {key}: '{value.value}'," for key, value in remapped.items())


print(
    "// WARNING: This file is auto-generated from src/waldur_core/permissions/print_permission_enums.py"
)
print("// Do not edit it manually. All manual changes would be overridden.")

print(
    f"""export const RoleEnum = {{
{format_enum(enums.RoleEnum)}
}};

export const PermissionMap = {{
{format_dict(enums.CREATE_PERMISSIONS)}
}};

export const PermissionEnum = {{
{format_enum(enums.PermissionEnum)}
}};

export const GlobalDeniedPermissions = [
{format_list(enums.GLOBAL_DENIED_PERMISSIONS)}
];"""
)
