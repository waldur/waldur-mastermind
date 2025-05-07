from django.utils.functional import cached_property

from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import OfferingStates, ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_slurm_remote import PLUGIN_NAME
from waldur_slurm.tests import factories as slurm_factories


class MarketplaceSlurmRemoteFixture(marketplace_fixtures.MarketplaceFixture):
    @cached_property
    def offering(self):
        return marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME,
            state=OfferingStates.ACTIVE,
            project=self.offering_project,
            customer=self.offering_customer,
        )

    @cached_property
    def resource(self):
        return marketplace_factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=self.project,
            scope=self.allocation,
        )

    @cached_property
    def allocation(self):
        return slurm_factories.AllocationFactory(
            project=self.offering_project,
        )


class GlauthUserFixture(marketplace_fixtures.MarketplaceFixture):
    def __init__(self):
        super().__init__()
        self.offering.type = PLUGIN_NAME
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True,
            "username_generation_policy": "waldur_username",
            "initial_uidnumber": 1000,
            "initial_primarygroup_number": 2000,
            "homedir_prefix": "/tmp/",
        }
        self.offering.save()

        # Set up resource and offering user
        self.resource.state = ResourceStates.OK
        self.resource.save()

        self.offering_user = marketplace_models.OfferingUser.objects.get(
            offering=self.offering, user=self.manager
        )
        self.offering_user.save()

        # Set up offering user groups
        self.offering_user_group1 = marketplace_models.OfferingUserGroup.objects.create(
            offering=self.offering, backend_metadata={"gid": 6001}
        )
        self.offering_user_group1.projects.set(
            [self.resource.project, self.offering_project]
        )
        self.offering_user_group1.save()

        self.offering_user_group2 = marketplace_models.OfferingUserGroup.objects.create(
            offering=self.offering, backend_metadata={"gid": 6002}
        )
        self.offering_user_group2.projects.set([self.offering_project])
        self.offering_user_group2.save()

        self.url = marketplace_factories.OfferingFactory.get_url(
            self.offering, "glauth_users_config"
        )

    @cached_property
    def offering_user_without_resources(self):
        # Create a new offering without any resources
        offering = marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME,
            customer=self.offering_customer,
            plugin_options={
                "service_provider_can_create_offering_user": True,
                "username_generation_policy": "waldur_username",
                "initial_uidnumber": 1000,
                "initial_primarygroup_number": 2000,
                "homedir_prefix": "/tmp/",
            },
        )
        offering_user = marketplace_models.OfferingUser.objects.create(
            offering=offering,
            user=self.manager,
            username="test_user",
        )
        marketplace_utils.setup_linux_related_data(offering_user, offering)
        offering_user.save()
        return offering_user

    @cached_property
    def offering_user_with_terminated_resource(self):
        # Create a new offering with a terminated resource
        offering = marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME,
            customer=self.offering_customer,
            plugin_options={
                "service_provider_can_create_offering_user": True,
                "username_generation_policy": "waldur_username",
                "initial_uidnumber": 1000,
                "initial_primarygroup_number": 2000,
                "homedir_prefix": "/tmp/",
            },
        )

        # Create a project and resource
        project = structure_factories.ProjectFactory(customer=self.offering_customer)
        marketplace_factories.ResourceFactory(
            offering=offering,
            project=project,
            state=ResourceStates.TERMINATED,
        )

        # Create offering user
        offering_user = marketplace_models.OfferingUser.objects.create(
            offering=offering,
            user=self.manager,
            username="test_user",
        )
        marketplace_utils.setup_linux_related_data(offering_user, offering)
        offering_user.save()

        # Add user to project
        project.add_user(self.manager, ProjectRole.ADMIN)

        return offering_user
