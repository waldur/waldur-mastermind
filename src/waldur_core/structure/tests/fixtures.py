from django.utils.functional import cached_property

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_mastermind.marketplace import tasks

from . import factories


def add_user_to_project(user, project, role=None):
    """Helper to add user to project and trigger offering user creation task."""
    if role is None:
        role = ProjectRole.MANAGER
    project.add_user(user, role)
    tasks.create_or_restore_offering_users_for_user(user.uuid.hex, project.uuid.hex)


class UserFixture:
    @cached_property
    def staff(self):
        return factories.UserFactory(is_staff=True)

    @cached_property
    def user(self):
        return factories.UserFactory()

    @cached_property
    def global_support(self):
        return factories.UserFactory(is_support=True)


class CustomerFixture(UserFixture):
    @cached_property
    def customer(self):
        return factories.CustomerFactory()

    @cached_property
    def owner(self):
        user = factories.UserFactory()
        self.customer.add_user(user, CustomerRole.OWNER)
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_PROJECTS)
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_ORDERS)
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_RESOURCES)
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_INVITATIONS)
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_CUSTOMER_USERS)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        CustomerRole.OWNER.add_permission(
            PermissionEnum.MANAGE_MAINTENANCE_ANNOUNCEMENT
        )
        return user

    @cached_property
    def customer_support(self):
        user = factories.UserFactory()
        self.customer.add_user(user, CustomerRole.SUPPORT)
        CustomerRole.SUPPORT.add_permission(PermissionEnum.LIST_PROJECTS)
        CustomerRole.SUPPORT.add_permission(PermissionEnum.LIST_ORDERS)
        CustomerRole.SUPPORT.add_permission(PermissionEnum.LIST_RESOURCES)
        CustomerRole.SUPPORT.add_permission(PermissionEnum.LIST_CUSTOMER_USERS)
        return user

    @cached_property
    def service_manager(self):
        user = factories.UserFactory()
        self.customer.add_user(user, ServiceProviderRole.MANAGER)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.MANAGE_MAINTENANCE_ANNOUNCEMENT
        )
        return user


class ProjectFixture(CustomerFixture):
    @cached_property
    def project(self):
        return factories.ProjectFactory(customer=self.customer)

    @cached_property
    def admin(self):
        admin = factories.UserFactory()
        add_user_to_project(admin, self.project, ProjectRole.ADMIN)
        ProjectRole.ADMIN.add_permission(PermissionEnum.LIST_PROJECTS)
        ProjectRole.ADMIN.add_permission(PermissionEnum.LIST_ORDERS)
        ProjectRole.ADMIN.add_permission(PermissionEnum.LIST_RESOURCES)
        return admin

    @cached_property
    def manager(self):
        manager = factories.UserFactory()
        add_user_to_project(manager, self.project, ProjectRole.MANAGER)
        ProjectRole.MANAGER.add_permission(PermissionEnum.LIST_PROJECTS)
        ProjectRole.MANAGER.add_permission(PermissionEnum.LIST_ORDERS)
        ProjectRole.MANAGER.add_permission(PermissionEnum.LIST_RESOURCES)

        ProjectRole.MANAGER.add_permission(PermissionEnum.REVIEW_PROJECT_MEMBERSHIP)
        return manager

    @cached_property
    def member(self):
        member = factories.UserFactory()
        add_user_to_project(member, self.project, ProjectRole.MEMBER)
        ProjectRole.MEMBER.add_permission(PermissionEnum.LIST_PROJECTS)
        ProjectRole.MEMBER.add_permission(PermissionEnum.LIST_ORDERS)
        ProjectRole.MEMBER.add_permission(PermissionEnum.LIST_RESOURCES)
        return member


class ServiceFixture(ProjectFixture):
    @cached_property
    def service_settings(self):
        return factories.ServiceSettingsFactory(customer=self.customer)

    @cached_property
    def resource(self):
        return factories.TestNewInstanceFactory(
            service_settings=self.service_settings, project=self.project
        )

    @cached_property
    def volume(self):
        return factories.TestVolumeFactory(
            service_settings=self.service_settings, project=self.project
        )

    @cached_property
    def snapshot(self):
        return factories.TestSnapshotFactory(
            service_settings=self.service_settings, project=self.project
        )
