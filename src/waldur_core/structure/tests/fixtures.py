from django.utils.functional import cached_property

from waldur_core.permissions.fixtures import CustomerRole, ProjectRole

from . import factories


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
        return user

    @cached_property
    def customer_support(self):
        user = factories.UserFactory()
        self.customer.add_user(user, CustomerRole.SUPPORT)
        return user

    @cached_property
    def service_manager(self):
        user = factories.UserFactory()
        self.customer.add_user(user, CustomerRole.MANAGER)
        return user


class ProjectFixture(CustomerFixture):
    @cached_property
    def project(self):
        return factories.ProjectFactory(customer=self.customer)

    @cached_property
    def admin(self):
        admin = factories.UserFactory()
        self.project.add_user(admin, ProjectRole.ADMIN)
        return admin

    @cached_property
    def manager(self):
        manager = factories.UserFactory()
        self.project.add_user(manager, ProjectRole.MANAGER)
        return manager

    @cached_property
    def member(self):
        member = factories.UserFactory()
        self.project.add_user(member, ProjectRole.MEMBER)
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
