import enums


def format_enum(enum):
    return "\n".join(
        f"  {key}: '{value.value}'," for key, value in enum._member_map_.items()
    )


def format_dict(enum):
    return "\n".join(f"  {key}: '{value.value}'," for key, value in enum.items())


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
