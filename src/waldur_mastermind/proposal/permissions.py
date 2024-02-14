from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import permission_factory

user_can_accept_requested_offering = permission_factory(
    PermissionEnum.ACCEPT_REQUESTED_OFFERING,
    ["offering.customer"],
)
