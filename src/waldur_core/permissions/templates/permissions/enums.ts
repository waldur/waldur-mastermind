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
