from django.test import TestCase

from waldur_freeipa.tests import factories as freeipa_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class UsernameGenerationTest(TestCase):
    def setUp(self) -> None:
        fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = fixture.offering
        self.user = fixture.user

    def test_username_is_empty_for_service_provider_case(self):
        self.offering.plugin_options = {
            'username_generation_policy': 'service_provider'
        }
        self.offering.save()

        username = utils.generate_username(self.user, self.offering)

        self.assertEqual(username, '')

    def test_username_generation_for_anonymized_case(self):
        self.offering.plugin_options = {
            'username_generation_policy': 'anonymized',
            'username_anonymized_prefix': 'anonymized_test_',
        }
        self.offering.save()

        username0 = utils.generate_username(self.user, self.offering)
        self.assertEqual(username0, 'anonymized_test_00000')
        marketplace_models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username=username0,
        )

        username1 = utils.generate_username(self.user, self.offering)
        self.assertEqual(username1, 'anonymized_test_00001')

    def test_username_generation_for_full_name_case(self):
        self.offering.plugin_options = {
            'username_generation_policy': 'full_name',
        }
        self.offering.save()
        self.user.first_name = 'Jöhn Karlos'
        self.user.last_name = 'Doe Jr'
        self.user.save()

        username0 = utils.generate_username(self.user, self.offering)
        marketplace_models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username=username0,
        )

        self.assertEqual(username0, 'john_karlos_doe_jr_00')

        username1 = utils.generate_username(self.user, self.offering)

        self.assertEqual(username1, 'john_karlos_doe_jr_01')

    def test_username_generation_for_waldur_username(self):
        self.offering.plugin_options = {
            'username_generation_policy': 'waldur_username',
        }
        self.offering.save()

        username = utils.generate_username(self.user, self.offering)

        self.assertEqual(username, self.user.username)

    def test_username_generation_for_freeipa(self):
        self.offering.plugin_options = {
            'username_generation_policy': 'freeipa',
        }
        self.offering.save()

        profile = freeipa_factories.ProfileFactory(user=self.user)

        username = utils.generate_username(self.user, self.offering)

        self.assertEqual(username, profile.username)
