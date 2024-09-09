from django.utils.functional import cached_property

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_slurm_remote import PLUGIN_NAME
from waldur_slurm.tests import factories as slurm_factories


class MarketplaceSlurmRemoteFixture(marketplace_fixtures.MarketplaceFixture):
    @cached_property
    def offering(self):
        return marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME,
            options={"order": []},
            state=marketplace_models.Offering.States.ACTIVE,
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
