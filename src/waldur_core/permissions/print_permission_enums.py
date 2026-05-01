import enums


def format_enum(enum):
    return "\n".join(
        f"  {key}: '{value.value}'," for key, value in enum._member_map_.items()
    )


# Keys exposed to the frontend must match the public TYPE_MAP / SDK RoleType
# convention (e.g. ``resource_project``), not Django's model_name
# (``resourceproject``). The Python-side dict still uses model_name because
# that's what callers look up via ``model_class._meta.model_name``.
_KEY_REMAP = {"resourceproject": "resource_project"}


def format_dict(enum):
    return "\n".join(
        f"  {_KEY_REMAP.get(key, key)}: '{value.value}'," for key, value in enum.items()
    )


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
}};"""
)
