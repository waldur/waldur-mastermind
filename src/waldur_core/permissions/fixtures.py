from django.contrib.contenttypes.models import ContentType

from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.models import Role


class CustomerRole:
    @classmethod
    @property
    def OWNER(self):
        return Role.objects.get_system_role(
            RoleEnum.CUSTOMER_OWNER,
            content_type=ContentType.objects.get_by_natural_key(
                "structure", "customer"
            ),
        )

    @classmethod
    @property
    def SUPPORT(self):
        return Role.objects.get_system_role(
            RoleEnum.CUSTOMER_SUPPORT,
            content_type=ContentType.objects.get_by_natural_key(
                "structure", "customer"
            ),
        )

    @classmethod
    @property
    def MANAGER(self):
        return Role.objects.get_system_role(
            RoleEnum.CUSTOMER_MANAGER,
            content_type=ContentType.objects.get_by_natural_key(
                "structure", "customer"
            ),
        )


class ProjectRole:
    @classmethod
    @property
    def ADMIN(self):
        return Role.objects.get_system_role(
            RoleEnum.PROJECT_ADMIN,
            content_type=ContentType.objects.get_by_natural_key("structure", "project"),
        )

    @classmethod
    @property
    def MANAGER(self):
        return Role.objects.get_system_role(
            RoleEnum.PROJECT_MANAGER,
            content_type=ContentType.objects.get_by_natural_key("structure", "project"),
        )

    @classmethod
    @property
    def MEMBER(self):
        return Role.objects.get_system_role(
            RoleEnum.PROJECT_MEMBER,
            content_type=ContentType.objects.get_by_natural_key("structure", "project"),
        )


class OfferingRole:
    @classmethod
    @property
    def MANAGER(self):
        return Role.objects.get_system_role(
            RoleEnum.OFFERING_MANAGER,
            content_type=ContentType.objects.get_by_natural_key(
                "marketplace", "offering"
            ),
        )


class CallRole:
    @classmethod
    @property
    def REVIEWER(self):
        return Role.objects.get_system_role(
            RoleEnum.CALL_REVIEWER,
            content_type=ContentType.objects.get_by_natural_key("proposal", "call"),
        )

    @classmethod
    @property
    def MANAGER(self):
        return Role.objects.get_system_role(
            RoleEnum.CALL_MANAGER,
            content_type=ContentType.objects.get_by_natural_key("proposal", "call"),
        )


class ProposalRole:
    @classmethod
    @property
    def MEMBER(self):
        return Role.objects.get_system_role(
            RoleEnum.PROPOSAL_MEMBER,
            content_type=ContentType.objects.get_by_natural_key("proposal", "proposal"),
        )
