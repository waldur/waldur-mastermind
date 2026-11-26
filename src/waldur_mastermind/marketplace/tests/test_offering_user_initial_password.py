"""The initial password the site agent stores on an offering user, and who reveals it."""

import uuid

from django.conf import settings
from django.db import connection
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.configuration.testing import override_config
from waldur_core.core import encryption
from waldur_core.core.tests.helpers import create_pat
from waldur_core.logging import enums as logging_enums
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories

IMPERSONATED_USER_HEADER = settings.WALDUR_CORE["REQUEST_HEADER_IMPERSONATED_USER_UUID"]
SET = logging_enums.EventType.MARKETPLACE_OFFERING_USER_INITIAL_PASSWORD_SET
REVEALED = logging_enums.EventType.MARKETPLACE_OFFERING_USER_INITIAL_PASSWORD_REVEALED
CLEARED = logging_enums.EventType.MARKETPLACE_OFFERING_USER_INITIAL_PASSWORD_CLEARED


class InitialPasswordTestBase(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()
        self.member = UserFactory()
        self.offering_user = models.OfferingUser.objects.create(
            offering=self.offering, user=self.member, username="ada@example.org"
        )
        # As permissions.yaml grants it: the site agent acts as an owner of the
        # provider's organization.
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)

    def url(self, action=None):
        url = factories.OfferingUserFactory.get_url(self.offering_user)
        return url + f"{action}/" if action else url

    def store(self, password="Xk3-initial.Wa1"):
        self.offering_user.initial_password_ciphertext = encryption.encrypt_value(
            password
        )
        self.offering_user.save(update_fields=["initial_password_ciphertext"])

    def set_password(self, user, password="Xk3-initial.Wa1", **extra):
        self.client.force_authenticate(user)
        return self.client.post(
            self.url("set_initial_password"), {"password": password, **extra}
        )

    def reveal(self, user):
        self.client.force_authenticate(user)
        return self.client.get(self.url("reveal_initial_password"))

    def clear(self, user):
        self.client.force_authenticate(user)
        return self.client.post(self.url("clear_initial_password"))

    def is_stored(self):
        self.offering_user.refresh_from_db()
        return bool(self.offering_user.initial_password_ciphertext)


class SetInitialPasswordTest(InitialPasswordTestBase):
    def test_the_provider_stores_it_encrypted(self):
        response = self.set_password(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        ciphertext = self.offering_user.initial_password_ciphertext
        self.assertNotIn("Xk3-initial.Wa1", ciphertext)
        self.assertEqual(encryption.decrypt_value(ciphertext), "Xk3-initial.Wa1")
        self.assertTrue(Event.objects.filter(event_type=SET).exists())

    def test_a_second_value_replaces_the_first(self):
        self.store("old-value")

        self.set_password(self.fixture.owner, "new-value")

        self.offering_user.refresh_from_db()
        self.assertEqual(
            encryption.decrypt_value(self.offering_user.initial_password_ciphertext),
            "new-value",
        )

    def test_an_offering_manager_holding_the_permission_stores_it(self):
        manager = UserFactory()
        self.offering.add_user(manager, OfferingRole.MANAGER)
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)

        response = self.set_password(manager)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_the_user_cannot_set_their_own(self):
        response = self.set_password(self.member)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.initial_password_ciphertext, "")

    def test_customer_support_cannot_set_it(self):
        response = self.set_password(self.fixture.customer_support)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_stored_when_the_username_it_was_created_for_still_matches(self):
        response = self.set_password(self.fixture.owner, username="ada@example.org")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(self.is_stored())

    def test_refused_when_the_username_changed_since_it_was_created(self):
        # The agent listed Ada as waiting under her old username; she was renamed
        # before its store arrived, so the password is for the replaced account.
        response = self.set_password(self.fixture.owner, username="ada@old.org")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(self.is_stored())
        self.assertFalse(Event.objects.filter(event_type=SET).exists())

    def test_refused_for_a_deleted_offering_user(self):
        self.offering_user.request_deletion()
        self.offering_user.set_deleting()
        self.offering_user.set_deleted()
        self.offering_user.save(update_fields=["state"])

        response = self.set_password(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(self.is_stored())

    def test_a_blank_password_is_rejected(self):
        response = self.set_password(self.fixture.owner, "")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class OfferingUserFieldTest(InitialPasswordTestBase):
    def test_the_offering_user_says_whether_one_is_stored_never_what(self):
        self.client.force_authenticate(self.fixture.owner)
        self.assertFalse(self.client.get(self.url()).data["has_initial_password"])

        self.store()
        data = self.client.get(self.url()).data

        self.assertTrue(data["has_initial_password"])
        self.assertNotIn("Xk3-initial.Wa1", str(data))
        self.assertNotIn("initial_password_ciphertext", data)

    def test_the_list_filters_on_whether_one_is_stored(self):
        waiting = models.OfferingUser.objects.create(
            offering=self.offering, user=UserFactory(), username="bob@example.org"
        )
        self.store()
        self.client.force_authenticate(self.fixture.owner)
        url = factories.OfferingUserFactory.get_list_url()

        def listed(value):
            response = self.client.get(
                url,
                {
                    "offering_uuid": self.offering.uuid.hex,
                    "has_initial_password": value,
                },
            )
            return [row["uuid"] for row in response.data]

        self.assertEqual(listed("true"), [self.offering_user.uuid.hex])
        self.assertEqual(listed("false"), [waiting.uuid.hex])


class RevealInitialPasswordTest(InitialPasswordTestBase):
    def test_the_user_reveals_their_own(self):
        self.store()

        response = self.reveal(self.member)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"password": "Xk3-initial.Wa1"})
        self.assertEqual(response["Cache-Control"], "no-store")
        event = Event.objects.get(event_type=REVEALED)
        self.assertIn(self.offering.name, event.message)

    def test_it_is_kept_after_a_reveal(self):
        self.store()

        self.reveal(self.member)
        response = self.reveal(self.member)

        self.assertEqual(response.data, {"password": "Xk3-initial.Wa1"})

    def test_nobody_else_reveals_it(self):
        self.store()
        for name in ("owner", "staff", "global_support", "customer_support"):
            with self.subTest(name):
                response = self.reveal(getattr(self.fixture, name))
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                self.assertNotIn("password", response.data or {})

    def test_not_by_staff_impersonating_the_user(self):
        self.store()
        token = Token.objects.get_or_create(user=self.fixture.staff)[0]
        self.client.credentials(
            **{
                "HTTP_AUTHORIZATION": f"Token {token.key}",
                IMPERSONATED_USER_HEADER: self.member.uuid.hex,
            }
        )

        response = self.client.get(self.url("reveal_initial_password"))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("impersonating", str(response.data))
        self.assertFalse(Event.objects.filter(event_type=REVEALED).exists())

    @override_config(PAT_ENABLED=True)
    def test_not_with_the_users_own_personal_access_token(self):
        self.store()
        self.member.can_use_personal_access_tokens = True
        self.member.save(update_fields=["can_use_personal_access_tokens"])
        _, token = create_pat(self.member, scopes=[])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        response = self.client.get(self.url("reveal_initial_password"))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("personal access token", str(response.data))
        self.assertFalse(Event.objects.filter(event_type=REVEALED).exists())

    def test_a_value_the_configured_keys_cannot_decrypt_is_a_conflict(self):
        self.offering_user.initial_password_ciphertext = "gAAAAABnot-a-valid-token"
        self.offering_user.save(update_fields=["initial_password_ciphertext"])

        response = self.reveal(self.member)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(Event.objects.filter(event_type=REVEALED).exists())

    def test_nothing_stored_is_a_conflict(self):
        response = self.reveal(self.member)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


class ClearInitialPasswordTest(InitialPasswordTestBase):
    def test_the_provider_clears_it(self):
        self.store()

        response = self.clear(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(self.is_stored())
        self.assertTrue(Event.objects.filter(event_type=CLEARED).exists())

    def test_clearing_nothing_succeeds_and_logs_nothing(self):
        response = self.clear(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(Event.objects.filter(event_type=CLEARED).exists())

    def test_the_user_and_customer_support_cannot_clear_it(self):
        self.store()
        for name, user in (
            ("member", self.member),
            ("customer_support", self.fixture.customer_support),
        ):
            with self.subTest(name):
                response = self.clear(user)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                self.assertTrue(self.is_stored())


class StaleInitialPasswordTest(InitialPasswordTestBase):
    def move(self, *transitions):
        for transition in transitions:
            getattr(self.offering_user, transition)()
            self.offering_user.save(update_fields=["state"])

    def test_kept_while_the_deletion_can_still_be_undone(self):
        self.store()

        self.move("request_deletion", "set_deleting")

        self.assertTrue(self.is_stored())

    def test_wiped_once_the_user_is_deleted(self):
        self.store()

        self.move("request_deletion", "set_deleting", "set_deleted")

        self.assertFalse(self.is_stored())
        self.assertFalse(Event.objects.filter(event_type=CLEARED).exists())

    def test_wiped_when_the_username_changes(self):
        for update_fields in (None, ["username"]):
            with self.subTest(update_fields=update_fields):
                self.store()

                self.offering_user.username = f"ada-{update_fields}@example.org"
                self.offering_user.save(update_fields=update_fields)

                self.assertFalse(self.is_stored())

    def test_wiped_by_an_instance_loaded_before_the_password_was_stored(self):
        def delete(stale):
            stale.request_deletion()
            stale.set_deleting()
            stale.set_deleted()
            stale.save(update_fields=["state"])

        def rename(stale):
            stale.username = "ada@new.example.org"
            stale.save(update_fields=["username"])

        for change in (rename, delete):
            with self.subTest(change.__name__):
                # Loaded while nothing was stored; the agent stores one meanwhile.
                stale = models.OfferingUser.objects.get(pk=self.offering_user.pk)
                self.store()

                change(stale)

                self.assertFalse(self.is_stored())

    def test_kept_when_the_first_username_is_assigned(self):
        self.offering_user = models.OfferingUser.objects.create(
            offering=self.offering, user=UserFactory(), username=""
        )
        self.store()

        self.offering_user.username = "grace@example.org"
        self.offering_user.save()

        self.assertTrue(self.is_stored())


class RollingDeployInsertTest(InitialPasswordTestBase):
    def test_insert_without_the_column_gets_the_database_default(self):
        # A pod still on the old code during a rolling deploy leaves the column out
        # of its INSERT; the database default must fill it in, or NOT NULL refuses it.
        table = models.OfferingUser._meta.db_table
        new_column = "initial_password_ciphertext"
        with connection.cursor() as cursor:
            columns = [
                column.name
                for column in connection.introspection.get_table_description(
                    cursor, table
                )
                if column.name not in ("id", "uuid", "user_id", "username", new_column)
            ]
            names = ", ".join(connection.ops.quote_name(c) for c in columns)
            cursor.execute(
                f"INSERT INTO {table} (uuid, user_id, username, {names}) "
                f"SELECT %s, %s, %s, {names} FROM {table} WHERE id = %s",
                [
                    uuid.uuid4().hex,
                    UserFactory().pk,
                    "grace@example.org",
                    self.offering_user.pk,
                ],
            )
        inserted = models.OfferingUser.objects.exclude(pk=self.offering_user.pk).get()
        self.assertEqual(inserted.initial_password_ciphertext, "")
