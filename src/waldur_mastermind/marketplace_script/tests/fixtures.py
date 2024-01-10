from django.utils.functional import cached_property
from rest_framework.reverse import reverse

from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures

from .. import PLUGIN_NAME


class ScriptFixture(marketplace_fixtures.MarketplaceFixture):
    @cached_property
    def offering(self):
        return marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME,
            options={"order": []},
            secret_options={
                "pull": "print('Resource got regular update')",
                "create": "print('Hello world!')",
                "delete": "print('Resource has been removed')",
                "update": "print('Resource has been changed')",
                "language": "python",
            },
        )

    @classmethod
    def get_dry_run_url(cls, offering):
        url = "http://testserver" + reverse(
            "marketplace-script-dry-run-detail", kwargs={"uuid": offering.uuid.hex}
        )
        return url + "run/"

    @classmethod
    def get_async_dry_run_url(cls, offering):
        url = "http://testserver" + reverse(
            "marketplace-script-dry-run-detail", kwargs={"uuid": offering.uuid.hex}
        )
        return url + "async_run/"
