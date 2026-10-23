import datetime
from io import StringIO
from unittest.mock import patch

import httpx
import respx
from ddt import data, ddt
from django.core.management import call_command
from rest_framework import status, test

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.enums import ProjectKind
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, tasks, utils
from waldur_mastermind.marketplace.enums import CourseAccountState, ServiceAccountState
from waldur_mastermind.marketplace.tests import factories, fixtures

TOKEN_URL = "http://example.com/api/token"
SERVICE_ACCOUNT_URL = "http://example.com/api/service-accounts"
COURSE_ACCOUNT_URL = "http://example.com/api/course-accounts"

# Each would leave its path segment, or be turned back into one that does by
# a proxy that decodes %2F before routing.
HOSTILE_USERNAMES = (".", "..", "a/../b", "../../admin/users", "x?y", "x#y", "a%2Fb")

ACCOUNT_SETTINGS = dict(
    SERVICE_ACCOUNT_USE_API=True,
    SERVICE_ACCOUNT_TOKEN_URL=TOKEN_URL,
    SERVICE_ACCOUNT_URL=SERVICE_ACCOUNT_URL,
    SERVICE_ACCOUNT_TOKEN_CLIENT_ID="client-id",
    SERVICE_ACCOUNT_TOKEN_SECRET="secret",
    COURSE_ACCOUNT_USE_API=True,
    COURSE_ACCOUNT_URL=COURSE_ACCOUNT_URL,
    COURSE_ACCOUNT_TOKEN_URL=TOKEN_URL,
    COURSE_ACCOUNT_TOKEN_CLIENT_ID="client-id",
    COURSE_ACCOUNT_TOKEN_SECRET="secret",
)


def create_response(username="new-sa"):
    service_account = {"status": "active"}
    if username is not None:
        service_account["username"] = username
    return {
        "serviceAccount": service_account,
        "apiKey": {"apiKey": "key", "expiresAt": "2030-01-01T00:00:00Z"},
    }


def paths():
    return [
        call.request.url.raw_path.decode()
        for call in respx.calls
        if call.request.url != TOKEN_URL
    ]


class AccountUsernameTest(test.APITestCase):
    def test_backend_style_usernames_are_kept_as_they_are(self):
        for username in ("svc-ci_bot.1", "course_00008", "A1"):
            self.assertEqual(
                utils.account_api_url(SERVICE_ACCOUNT_URL, username, "close"),
                f"{SERVICE_ACCOUNT_URL}/{username}/close",
            )

    def test_hostile_and_empty_usernames_are_refused(self):
        for username in HOSTILE_USERNAMES + ("", None, "has space"):
            with self.assertRaises(utils.InvalidAccountUsername, msg=username):
                utils.account_api_url(SERVICE_ACCOUNT_URL, username)

    def test_length_is_checked_against_the_column(self):
        utils.validate_service_account_username("a" * 32)
        with self.assertRaises(utils.InvalidAccountUsername):
            utils.validate_service_account_username("a" * 33)


class BaseAccountApiTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        ProjectRole.MANAGER.add_permission(PermissionEnum.MANAGE_SERVICE_ACCOUNT)
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_SERVICE_ACCOUNT)
        self.create_response = create_response()
        respx.start()
        # Undone even if the rest of setUp fails, so the router never leaks
        # into later tests on this worker.
        self.addCleanup(respx.stop)
        respx.route().mock(side_effect=self.backend)

    def backend(self, request):
        if request.url == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "token"})
        if request.method == "POST":
            return httpx.Response(200, json=self.create_response)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "serviceAccount": {"status": "active"},
                    "tempAccount": {"status": "active"},
                },
            )
        return httpx.Response(200, json=create_response())

    def service_account(self, username, project=None, **kwargs):
        return factories.ProjectServiceAccountFactory(
            project=project or self.fixture.project, username=username, **kwargs
        )

    def url(self, account):
        return factories.ProjectServiceAccountFactory.get_url(account)


@override_waldur_core_settings(**ACCOUNT_SETTINGS)
@ddt
class HostileUsernameNeverReachesTheBackendTest(BaseAccountApiTest):
    @data(*HOSTILE_USERNAMES)
    def test_get_update_rotate_refuse_without_a_request(self, username):
        account = self.service_account(username)

        for call in (
            utils.get_service_account,
            utils.update_service_account,
            utils.rotate_service_account_api_key,
        ):
            with self.assertRaises(utils.InvalidAccountUsername):
                call(account)

        self.assertEqual(paths(), [])

    @data(*HOSTILE_USERNAMES)
    def test_close_closes_in_waldur_only_and_says_why(self, username):
        account = self.service_account(username)

        utils.close_service_account(account)

        self.assertEqual(paths(), [])
        account.refresh_from_db()
        self.assertEqual(account.state, ServiceAccountState.CLOSED)
        self.assertTrue(account.error_message.startswith("Closed in Waldur only"))

    @data(*HOSTILE_USERNAMES)
    def test_close_by_username_refuses_without_a_request(self, username):
        with self.assertRaises(utils.InvalidAccountUsername):
            utils.close_service_account_by_username(username)
        with self.assertRaises(utils.InvalidAccountUsername):
            utils.close_course_account_by_username(username)

        self.assertEqual(paths(), [])

    def test_course_account_closes_in_waldur_only_and_says_why(self):
        project = structure_factories.ProjectFactory(kind=ProjectKind.COURSE)
        user = structure_factories.UserFactory(username="../../x")
        account = factories.CourseAccountFactory(project=project, user=user)

        utils.close_course_account(account)

        self.assertEqual(paths(), [])
        account.refresh_from_db()
        self.assertEqual(account.state, CourseAccountState.CLOSED)
        self.assertTrue(account.error_message.startswith("Closed in Waldur only"))

    def test_api_update_and_rotate_answer_400_and_delete_closes_locally(self):
        self.client.force_authenticate(self.fixture.manager)

        update = self.client.patch(
            self.url(self.service_account("..")), {"description": "x"}
        )
        rotate = self.client.post(
            f"{self.url(self.service_account('..'))}rotate_api_key/"
        )
        doomed = self.service_account("..")
        delete = self.client.delete(self.url(doomed))

        self.assertEqual(update.status_code, status.HTTP_400_BAD_REQUEST, update.data)
        self.assertEqual(rotate.status_code, status.HTTP_400_BAD_REQUEST, rotate.data)
        self.assertEqual(delete.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(paths(), [])
        doomed.refresh_from_db()
        self.assertEqual(doomed.state, ServiceAccountState.CLOSED)

    def test_other_value_errors_are_not_turned_into_a_400(self):
        # A rotated key in a body that isn't JSON: the old key is gone, and
        # the caller must not be told their request was wrong.
        account = self.service_account("own-sa")
        respx.clear()
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "token"})
        )
        respx.put(f"{SERVICE_ACCOUNT_URL}/own-sa/rotate-api-key").mock(
            return_value=httpx.Response(200, text="<html>")
        )
        self.client.force_authenticate(self.fixture.manager)
        self.client.raise_request_exception = False

        response = self.client.post(f"{self.url(account)}rotate_api_key/")

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)


@override_waldur_core_settings(**ACCOUNT_SETTINGS)
@ddt
class ServiceAccountIdentityIsFixedTest(BaseAccountApiTest):
    """A manager can't point their own service account at another tenant's."""

    def setUp(self):
        super().setUp()
        self.victim_project = structure_factories.ProjectFactory()
        self.client.force_authenticate(self.fixture.manager)

    @data("victim-sa", "../../admin/users", "x?y")
    def test_patch_leaves_username_unchanged(self, username):
        account = self.service_account("own-sa")

        response = self.client.patch(self.url(account), {"username": username})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["username"], "own-sa")
        account.refresh_from_db()
        self.assertEqual(account.username, "own-sa")

    def test_patch_leaves_project_unchanged(self):
        account = self.service_account("own-sa")

        for project in (self.victim_project.uuid.hex, None):
            response = self.client.patch(
                self.url(account), {"project": project}, format="json"
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            account.refresh_from_db()
            self.assertEqual(account.project, self.fixture.project)

    def test_patch_leaves_preferred_identifier_unchanged(self):
        account = self.service_account("own-sa", preferred_identifier="own")

        response = self.client.patch(
            self.url(account), {"preferred_identifier": "changed"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        account.refresh_from_db()
        self.assertEqual(account.preferred_identifier, "own")

    def test_customer_account_username_and_customer_are_fixed_too(self):
        account = factories.CustomerServiceAccountFactory(
            customer=self.fixture.customer, username="own-sa"
        )
        victim = structure_factories.CustomerFactory()
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.patch(
            factories.CustomerServiceAccountFactory.get_url(account),
            {"username": "victim-sa", "customer": victim.uuid.hex},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["username"], "own-sa")
        account.refresh_from_db()
        self.assertEqual(
            (account.username, account.customer), ("own-sa", self.fixture.customer)
        )

    def test_issue_reproduction_closes_only_the_managers_own_account(self):
        account = self.service_account("own-sa")

        patch_response = self.client.patch(self.url(account), {"username": "victim-sa"})
        self.assertEqual(patch_response.status_code, status.HTTP_200_OK)
        self.assertEqual(patch_response.data["username"], "own-sa")
        response = self.client.delete(self.url(account))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertNotIn("victim-sa", " ".join(paths()))
        self.assertIn("/api/service-accounts/own-sa/close", paths())


@override_waldur_core_settings(**ACCOUNT_SETTINGS)
class SharedUsernameIsRefusedTest(BaseAccountApiTest):
    """Rows hijacked before the fix: two open accounts claim one username."""

    def setUp(self):
        super().setUp()
        self.victim = self.service_account(
            "victim-sa", project=structure_factories.ProjectFactory()
        )
        self.hijacked = self.service_account("victim-sa")

    def test_close_update_rotate_are_refused_without_a_request(self):
        for call in (
            utils.close_service_account,
            utils.update_service_account,
            utils.rotate_service_account_api_key,
        ):
            with self.assertRaises(utils.AccountUsernameConflict):
                call(self.hijacked)

        self.assertEqual(paths(), [])
        self.hijacked.refresh_from_db()
        self.assertEqual(self.hijacked.state, ServiceAccountState.ERRED)

    def test_api_answers_400(self):
        self.client.force_authenticate(self.fixture.manager)
        url = self.url(self.hijacked)

        def reset():
            # A refused call leaves the row ERRED, and rotate refuses an
            # ERRED account before it gets as far as the guard.
            models.ProjectServiceAccount.objects.filter(pk=self.hijacked.pk).update(
                state=ServiceAccountState.OK
            )

        responses = []
        for call in (
            lambda: self.client.patch(url, {"description": "x"}),
            lambda: self.client.post(f"{url}rotate_api_key/"),
            lambda: self.client.delete(url),
        ):
            reset()
            responses.append(call())

        self.assertEqual(
            [response.status_code for response in responses],
            [status.HTTP_400_BAD_REQUEST] * 3,
        )
        self.assertEqual(paths(), [])

    def test_a_closed_duplicate_no_longer_blocks(self):
        models.ProjectServiceAccount.objects.filter(pk=self.hijacked.pk).update(
            state=ServiceAccountState.CLOSED
        )

        utils.close_service_account(self.victim)

        self.assertIn("/api/service-accounts/victim-sa/close", paths())

    def test_close_by_username_is_refused_while_an_open_account_claims_it(self):
        # Closing by username means the row is gone, so any open row still
        # claiming the username is the one that owns the backend account.
        with self.assertRaises(utils.AccountUsernameConflict):
            utils.close_service_account_by_username("victim-sa")

        self.assertEqual(paths(), [])

    def test_deleting_a_hijacked_customer_leaves_the_victims_account_open(self):
        customer = structure_factories.CustomerFactory()
        factories.CustomerServiceAccountFactory(customer=customer, username="victim-sa")

        # conftest runs on_commit callbacks at once, while the row still
        # exists. Run them after the delete instead, as a commit would: the
        # CASCADE has removed the row, so the task closes by username.
        with patch("django.db.transaction.on_commit") as on_commit:
            customer.delete()
        self.assertFalse(models.CustomerServiceAccount.objects.exists())
        with patch.object(
            tasks.close_service_accounts_task,
            "delay",
            side_effect=tasks.close_service_accounts_task,
        ):
            for callback in on_commit.call_args_list:
                callback.args[0]()

        self.assertNotIn("victim-sa", " ".join(paths()))
        self.victim.refresh_from_db()
        self.assertEqual(self.victim.state, ServiceAccountState.OK)

    def test_close_by_username_goes_through_once_no_open_account_claims_it(self):
        models.ProjectServiceAccount.objects.filter(
            pk__in=[self.victim.pk, self.hijacked.pk]
        ).update(state=ServiceAccountState.CLOSED)

        utils.close_service_account_by_username("victim-sa")

        self.assertIn("/api/service-accounts/victim-sa/close", paths())

    def test_check_command_lists_shared_and_invalid_usernames(self):
        self.service_account("../../admin/users")
        out = StringIO()

        call_command("check_service_account_usernames", stdout=out)

        output = out.getvalue()
        self.assertIn("Invalid usernames: 1", output)
        self.assertIn("'../../admin/users'", output)
        self.assertIn("Usernames claimed by more than one account: 1", output)
        self.assertIn(self.victim.uuid.hex, output)
        self.assertIn(self.hijacked.uuid.hex, output)

    def test_check_command_lists_accounts_closed_in_waldur_only(self):
        account = self.service_account("../../admin/users")
        utils.close_service_account(account)
        out = StringIO()

        call_command("check_service_account_usernames", stdout=out)

        output = out.getvalue()
        self.assertIn("Closed in Waldur only: 1", output)
        self.assertIn(account.uuid.hex, output)


@override_waldur_core_settings(**ACCOUNT_SETTINGS)
@ddt
class BackendUsernameIsCheckedOnCreateTest(BaseAccountApiTest):
    def create(self, preferred_identifier="ci"):
        self.client.force_authenticate(self.fixture.manager)
        return self.client.post(
            factories.ProjectServiceAccountFactory.get_list_url(),
            {
                "project": self.fixture.project.uuid.hex,
                "description": "ci",
                "preferred_identifier": preferred_identifier,
            },
        )

    def test_a_good_username_is_stored(self):
        response = self.create()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["username"], "new-sa")
        self.assertEqual(response.data["token"], "key")

    @data(*HOSTILE_USERNAMES, "my bot")
    def test_an_unusable_preferred_identifier_never_reaches_the_backend(
        self, identifier
    ):
        # The backend may name the account after it, and an account named
        # outside the allowlist could be neither stored nor closed by Waldur.
        response = self.create(preferred_identifier=identifier)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("preferred_identifier", response.data)
        self.assertEqual(paths(), [])
        self.assertFalse(models.ProjectServiceAccount.objects.exists())

    @data("", "ci-bot_1.x")
    def test_an_empty_or_backend_style_preferred_identifier_is_accepted(
        self, identifier
    ):
        response = self.create(preferred_identifier=identifier)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @data(None, "", "..", "../../admin/users")
    def test_an_unusable_username_fails_the_create(self, username):
        self.create_response = create_response(username)

        response = self.create()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("token", response.data)
        self.assertFalse(models.ProjectServiceAccount.objects.exists())
        # Nothing can address such an account, so nothing is sent for it.
        self.assertFalse(any(p.endswith("/close") for p in paths()))

    def test_a_too_long_username_fails_the_create_and_is_closed(self):
        long_name = "a" * 40
        self.create_response = create_response(long_name)

        response = self.create()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(models.ProjectServiceAccount.objects.exists())
        self.assertIn(f"/api/service-accounts/{long_name}/close", paths())

    def test_course_account_task_refuses_an_unusable_username(self):
        project = structure_factories.ProjectFactory(kind=ProjectKind.COURSE)
        account = factories.CourseAccountFactory(
            project=project, state=CourseAccountState.PENDING
        )

        with patch.object(
            utils,
            "create_course_account",
            return_value={"tempAccount": {"username": "../../x"}},
        ):
            tasks.create_course_account_task(account.uuid.hex, "owner")

        account.refresh_from_db()
        self.assertEqual(account.state, CourseAccountState.ERRED)
        self.assertIn("Invalid account username", account.error_message)

    def test_a_too_long_course_username_fails_the_task_and_is_closed(self):
        project = structure_factories.ProjectFactory(kind=ProjectKind.COURSE)
        account = factories.CourseAccountFactory(
            project=project, state=CourseAccountState.PENDING
        )
        long_name = "a" * 129

        with patch.object(
            utils,
            "create_course_account",
            return_value={"tempAccount": {"username": long_name}},
        ):
            tasks.create_course_account_task(account.uuid.hex, "owner")

        account.refresh_from_db()
        self.assertEqual(account.state, CourseAccountState.ERRED)
        # The backend's name for it, so staff can find it there.
        self.assertIn(long_name, account.error_message)
        self.assertIn(f"/api/course-accounts/{long_name}/close", paths())

    def test_a_too_long_course_username_fails_the_api_create_and_is_closed(self):
        long_name = "a" * 129
        self.create_response = {
            "tempAccount": {"username": long_name, "email": "ca@example.com"}
        }
        project = structure_factories.ProjectFactory(
            kind=ProjectKind.COURSE,
            end_date=datetime.date.today() + datetime.timedelta(days=30),
        )
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            factories.CourseAccountFactory.get_list_url(),
            {"project": project.uuid.hex, "email": "ca@example.com"},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(models.CourseAccount.objects.filter(project=project).exists())
        self.assertIn(f"/api/course-accounts/{long_name}/close", paths())
