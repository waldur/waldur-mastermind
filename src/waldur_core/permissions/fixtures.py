from django.contrib.contenttypes.models import ContentType

from waldur_core.core.types import classproperty
from waldur_core.permissions.enums import TYPE_MAP, RoleEnum
from waldur_core.permissions.models import Role


class CustomerRole:
    @classproperty
    def OWNER(self):
        return Role.objects.get_system_role(
            RoleEnum.CUSTOMER_OWNER,
            content_type=ContentType.objects.get_by_natural_key(
                "structure", "customer"
            ),
        )

    @classproperty
    def SUPPORT(self):
        return Role.objects.get_system_role(
            RoleEnum.CUSTOMER_SUPPORT,
            content_type=ContentType.objects.get_by_natural_key(
                "structure", "customer"
            ),
        )

    @classproperty
    def READER(self):
        return Role.objects.get_system_role(
            RoleEnum.CUSTOMER_READER,
            content_type=ContentType.objects.get_by_natural_key(
                "structure", "customer"
            ),
        )


class ServiceProviderRole:
    @classproperty
    def MANAGER(self):
        return Role.objects.get_system_role(
            RoleEnum.CUSTOMER_MANAGER,
            content_type=ContentType.objects.get_by_natural_key(
                "marketplace", "serviceprovider"
            ),
        )


class ProjectRole:
    @classproperty
    def ADMIN(self):
        return Role.objects.get_system_role(
            RoleEnum.PROJECT_ADMIN,
            content_type=ContentType.objects.get_by_natural_key("structure", "project"),
        )

    @classproperty
    def MANAGER(self):
        return Role.objects.get_system_role(
            RoleEnum.PROJECT_MANAGER,
            content_type=ContentType.objects.get_by_natural_key("structure", "project"),
        )

    @classproperty
    def MEMBER(self):
        return Role.objects.get_system_role(
            RoleEnum.PROJECT_MEMBER,
            content_type=ContentType.objects.get_by_natural_key("structure", "project"),
        )


class OfferingRole:
    @classproperty
    def MANAGER(self):
        return Role.objects.get_system_role(
            RoleEnum.OFFERING_MANAGER,
            content_type=ContentType.objects.get_by_natural_key(
                "marketplace", "offering"
            ),
        )


class CallRole:
    @classproperty
    def REVIEWER(self):
        return Role.objects.get_system_role(
            RoleEnum.CALL_REVIEWER,
            content_type=ContentType.objects.get_by_natural_key("proposal", "call"),
        )

    @classproperty
    def MANAGER(self):
        return Role.objects.get_system_role(
            RoleEnum.CALL_MANAGER,
            content_type=ContentType.objects.get_by_natural_key("proposal", "call"),
        )

    @classproperty
    def PANEL_MEMBER(self):
        return Role.objects.get_system_role(
            RoleEnum.CALL_PANEL_MEMBER,
            content_type=ContentType.objects.get_by_natural_key("proposal", "call"),
        )


class ProposalRole:
    @classproperty
    def MEMBER(self):
        return Role.objects.get_system_role(
            RoleEnum.PROPOSAL_MEMBER,
            content_type=ContentType.objects.get_by_natural_key("proposal", "proposal"),
        )

    @classproperty
    def MANAGER(self):
        return Role.objects.get_system_role(
            RoleEnum.PROPOSAL_MANAGER,
            content_type=ContentType.objects.get_by_natural_key("proposal", "proposal"),
        )

    @classproperty
    def ADMIN(self):
        return Role.objects.get_system_role(
            RoleEnum.PROPOSAL_ADMIN,
            content_type=ContentType.objects.get_by_natural_key("proposal", "proposal"),
        )


class GlobalRole:
    """A deployment-defined role on the site-wide scope.

    The name is chosen by the deployment, the same way ``import_roles``
    creates one from ``custom-roles.yaml`` with ``scope: global``. Grant it
    with ``grant_global_role(user, role)`` after ``add_permission``.
    """

    @staticmethod
    def get(name):
        return Role.objects.get_system_role(
            name,
            content_type=ContentType.objects.get_by_natural_key(*TYPE_MAP["global"]),
        )
