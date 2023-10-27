from jinja2 import Template

from . import enums

template = Template(
    """
/* eslint-disable */
// WARNING: This file is auto-generated from src/waldur_core/permissions/enums.py
// Do not edit it manually. All manual changes would be overridden.

export const RoleEnum = {
{% for key, value in roles %}
  {{ key }}: '{{ value.value }}',
{% endfor %}};

export const PermissionEnum = {
{% for key, value in permissions %}
  {{ key }}: '{{ value.value }}',
{% endfor %}};
"""
)

context = {
    'roles': enums.RoleEnum._member_map_.items(),
    'permissions': enums.PermissionEnum._member_map_.items(),
}
print(template.render(context).replace('\n\n', '\n'))
