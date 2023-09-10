# This is temporary code. It it intended for transitional phase only.
from waldur_core.permissions.enums import RoleEnum
from waldur_core.structure.models import Customer, CustomerRole, Project, ProjectRole

ROLE_MAP = {
    (Customer, CustomerRole.OWNER): RoleEnum.CUSTOMER_OWNER,
    (Customer, CustomerRole.SERVICE_MANAGER): RoleEnum.CUSTOMER_MANAGER,
    (Customer, CustomerRole.SUPPORT): RoleEnum.CUSTOMER_SUPPORT,
    (Project, ProjectRole.ADMINISTRATOR): RoleEnum.PROJECT_ADMIN,
    (Project, ProjectRole.MANAGER): RoleEnum.PROJECT_MANAGER,
    (Project, ProjectRole.MEMBER): RoleEnum.PROJECT_MEMBER,
}
