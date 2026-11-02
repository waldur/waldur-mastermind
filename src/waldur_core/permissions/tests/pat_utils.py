"""Authenticate a test client with a personal access token.

For tests of the staff and support scopes: a staff or support user's token
without `STAFF.ACCESS` or `SUPPORT.ACCESS` must act as the user without that
role (#945).
"""

from rest_framework.authtoken.models import Token

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.tests.test_pat_list_filtering import (
    _auth_header,
    _create_pat,
)

STAFF = PermissionEnum.STAFF_ACCESS.value
SUPPORT = PermissionEnum.SUPPORT_ACCESS.value
# A scope that no endpoint under test asks for.
UNRELATED = PermissionEnum.LIST_ORDERS.value


def use_pat(client, user, *scopes, bindings=()):
    """Authenticate ``client`` as ``user`` through a new token carrying
    ``scopes`` (an unrelated one if none are given)."""
    if not user.can_use_personal_access_tokens:
        user.can_use_personal_access_tokens = True
        user.save(update_fields=["can_use_personal_access_tokens"])
    Token.objects.get_or_create(user=user)
    pat = _create_pat(user, scopes=list(scopes) or [UNRELATED], bindings=bindings)
    client.force_authenticate(None)
    client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
    return pat
