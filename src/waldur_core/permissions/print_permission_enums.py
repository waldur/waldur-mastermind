import enums


def format_enum(enum):
    return "\n".join(
        f"  {key}: '{value.value}'," for key, value in enum._member_map_.items()
    )


print(
    f"""/* eslint-disable */
// WARNING: This file is auto-generated from src/waldur_core/permissions/enums.py
// Do not edit it manually. All manual changes would be overridden.

export const RoleEnum = {{
{format_enum(enums.RoleEnum)}
}};

export const PermissionEnum = {{
{format_enum(enums.PermissionEnum)}
}};"""
)
