import uuid
from datetime import timedelta
from unittest.mock import patch

import httpx
import respx
from celery.exceptions import SoftTimeLimitExceeded
from celery.schedules import crontab
from django.db import transaction
from django.utils import timezone
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.structure.enums import ProjectKind
from waldur_core.structure.models import Project
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, tasks, utils
from waldur_mastermind.marketplace.enums import (
    CourseAccountState,
    ServiceAccountState,
)
from waldur_mastermind.marketplace.extension import MarketplaceExtension
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.marketplace.tests.utils import real_on_commit

TOKEN_URL = "http://example.com/api/token"
SERVICE_ACCOUNT_URL = "http://example.com/api/service-accounts"
COURSE_ACCOUNT_URL = "http://example.com/api/course-accounts"


def service_account_response(username, status="active"):
    return {"serviceAccount": {"username": username, "status": status}}


def course_account_response(username, status="active"):
    return {"tempAccount": {"username": username, "status": status}}


def calls(method, suffix=""):
    return [
        call
        for call in respx.calls
        if call.request.method == method and str(call.request.url).endswith(suffix)
    ]


class BaseClosingTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        respx.start()
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "token"})
        )

    def tearDown(self):
        respx.stop()
        respx.clear()
        super().tearDown()


@override_waldur_core_settings(
    SERVICE_ACCOUNT_USE_API=True,
    SERVICE_ACCOUNT_TOKEN_URL=TOKEN_URL,
    SERVICE_ACCOUNT_URL=SERVICE_ACCOUNT_URL,
    SERVICE_ACCOUNT_TOKEN_CLIENT_ID="client-id",
    SERVICE_ACCOUNT_TOKEN_SECRET="secret",
)
class ServiceAccountClosingTest(BaseClosingTest):
    def setUp(self):
        super().setUp()
        self.project = self.fixture.project

    def make_account(self, username, **kwargs):
        kwargs.setdefault("project", self.project)
        return factories.ProjectServiceAccountFactory(username=username, **kwargs)

    def mock_backend(self, username, status="active", close_status=200):
        respx.get(f"{SERVICE_ACCOUNT_URL}/{username}").mock(
            return_value=httpx.Response(
                200, json=service_account_response(username, status)
            )
        )
        respx.put(f"{SERVICE_ACCOUNT_URL}/{username}/close").mock(
            return_value=httpx.Response(close_status, json={})
        )

    def delete_project(self, **kwargs):
        # The task runs once the delete is done, as after a commit: a hard
        # delete has CASCADEd the rows away by then, so it closes by username.
        with (
            real_on_commit(),
            patch.object(
                tasks.close_service_accounts_task,
                "delay",
                side_effect=tasks.close_service_accounts_task,
            ),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                self.project.delete(**kwargs)

    def test_project_deletion_closes_accounts_after_commit_with_one_token(self):
        first = self.make_account("sa-one")
        second = self.make_account("sa-two")
        self.mock_backend("sa-one")
        self.mock_backend("sa-two")

        with (
            real_on_commit(),
            patch.object(tasks.close_service_accounts_task, "delay") as delay,
        ):
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                self.project.delete()
            # Nothing is sent while the delete is still in its transaction.
            self.assertEqual(calls("PUT"), [])
            delay.assert_not_called()
            for callback in callbacks:
                callback()
        delay.assert_called_once()

        tasks.close_service_accounts_task(*delay.call_args.args)

        self.assertEqual(len(calls("PUT", "/close")), 2)
        self.assertEqual(len(calls("POST")), 1)
        self.assertFalse(
            models.ProjectServiceAccount.objects.filter(
                pk__in=[first.pk, second.pk]
            ).exists()
        )

    def test_rolled_back_project_deletion_does_not_close_accounts(self):
        account = self.make_account("sa-rollback")
        self.mock_backend("sa-rollback")

        with (
            real_on_commit(),
            patch.object(tasks.close_service_accounts_task, "delay") as delay,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        self.project.delete()
                        raise RuntimeError("deletion failed partway")
                except RuntimeError:
                    pass

        delay.assert_not_called()
        self.assertEqual(calls("PUT"), [])
        account.refresh_from_db()
        self.assertEqual(account.state, ServiceAccountState.OK)

    def test_backend_failure_leaves_account_erred_in_db(self):
        account = self.make_account("sa-fails")
        self.mock_backend("sa-fails", close_status=500)

        self.delete_project()

        account.refresh_from_db()
        self.assertEqual(account.state, ServiceAccountState.ERRED)
        self.assertTrue(account.error_message)

    def test_token_failure_leaves_accounts_erred_not_ok(self):
        first = self.make_account("sa-token-one")
        second = self.make_account("sa-token-closed", state=ServiceAccountState.CLOSED)
        respx.clear()
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(500))

        self.delete_project()

        self.assertEqual(calls("PUT"), [])
        first.refresh_from_db()
        self.assertEqual(first.state, ServiceAccountState.ERRED)
        self.assertIn("token", first.error_message)
        # The closed one needed no backend call, so its row goes as usual.
        self.assertFalse(
            models.ProjectServiceAccount.objects.filter(pk=second.pk).exists()
        )

    def test_token_failure_stores_no_backend_url(self):
        account = self.make_account("sa-token-401")
        respx.clear()
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(401))

        self.delete_project()

        account.refresh_from_db()
        self.assertEqual(account.error_message, tasks.TOKEN_FAILURE_MESSAGE)
        self.assertNotIn("http", account.error_message)

    def test_token_failure_still_closes_account_without_username_locally(self):
        named = self.make_account("sa-token-named")
        blank = self.make_account("")
        respx.clear()
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(500))

        self.delete_project()

        named.refresh_from_db()
        self.assertEqual(named.state, ServiceAccountState.ERRED)
        self.assertFalse(
            models.ProjectServiceAccount.objects.filter(pk=blank.pk).exists()
        )

    def test_unexpected_failure_leaves_account_erred(self):
        account = self.make_account("sa-unexpected")

        with patch.object(
            utils, "close_service_account", side_effect=RuntimeError("boom")
        ):
            self.delete_project()

        account.refresh_from_db()
        self.assertEqual(account.state, ServiceAccountState.ERRED)
        self.assertEqual(account.error_message, "boom")

    def test_account_already_closed_at_backend_is_closed_locally(self):
        account = self.make_account(
            "sa-closed",
            state=ServiceAccountState.ERRED,
            error_message="Service account 'sa-closed' is already closed",
        )
        self.mock_backend("sa-closed", status="closed", close_status=400)

        utils.close_service_account(account)

        self.assertEqual(calls("PUT"), [])
        account.refresh_from_db()
        self.assertEqual(account.state, ServiceAccountState.CLOSED)
        self.assertEqual(account.error_message, "")
        self.assertEqual(account.error_traceback, "")

    def test_account_missing_at_backend_is_closed_locally(self):
        account = self.make_account("sa-missing")
        respx.get(f"{SERVICE_ACCOUNT_URL}/sa-missing").mock(
            return_value=httpx.Response(404)
        )

        utils.close_service_account(account)

        self.assertEqual(calls("PUT"), [])
        account.refresh_from_db()
        self.assertEqual(account.state, ServiceAccountState.CLOSED)

    def test_project_deletion_with_account_already_closed_at_backend(self):
        account = self.make_account("sa-closed")
        self.mock_backend("sa-closed", status="closed", close_status=400)

        self.delete_project()

        self.assertEqual(calls("PUT"), [])
        self.assertFalse(
            models.ProjectServiceAccount.objects.filter(pk=account.pk).exists()
        )

    def test_destroy_of_account_already_closed_at_backend_succeeds(self):
        account = self.make_account("sa-closed")
        self.mock_backend("sa-closed", status="closed", close_status=400)
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.delete(
            factories.ProjectServiceAccountFactory.get_url(account)
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(calls("PUT"), [])
        account.refresh_from_db()
        self.assertEqual(account.state, ServiceAccountState.CLOSED)

    def test_locally_closed_account_is_not_sent_again(self):
        account = self.make_account("sa-done", state=ServiceAccountState.CLOSED)

        self.delete_project()

        # Not even a token: nothing in the batch needs the backend.
        self.assertEqual(respx.calls.call_count, 0)
        self.assertFalse(
            models.ProjectServiceAccount.objects.filter(pk=account.pk).exists()
        )

    def test_hard_project_deletion_skips_account_without_username(self):
        self.make_account("")

        self.delete_project(soft=False)

        self.assertEqual(calls("GET"), [])
        self.assertEqual(calls("PUT"), [])

    def test_soft_project_deletion_closes_account_without_username_locally(self):
        account = self.make_account("")

        self.delete_project()

        # Not the collection endpoint, and no token either.
        self.assertEqual(respx.calls.call_count, 0)
        self.assertFalse(
            models.ProjectServiceAccount.objects.filter(pk=account.pk).exists()
        )

    def test_account_without_username_is_closed_locally(self):
        account = self.make_account("")

        utils.close_service_account(account)

        self.assertEqual(respx.calls.call_count, 0)
        account.refresh_from_db()
        self.assertEqual(account.state, ServiceAccountState.CLOSED)

    def test_username_outside_one_path_segment_is_never_sent(self):
        with self.assertRaises(utils.InvalidAccountUsername):
            utils.close_service_account_by_username("a/../b?c#d")

        self.assertEqual(calls("GET") + calls("PUT"), [])

    def test_hard_project_deletion_closes_accounts_by_username(self):
        self.make_account("sa-hard")
        self.mock_backend("sa-hard")

        self.delete_project(soft=False)

        self.assertEqual(len(calls("PUT", "/sa-hard/close")), 1)

    def test_hard_project_deletion_skips_accounts_closed_at_backend(self):
        self.make_account("sa-hard-closed")
        self.mock_backend("sa-hard-closed", status="closed", close_status=400)

        self.delete_project(soft=False)

        self.assertEqual(calls("PUT"), [])

    def test_customer_deletion_closes_customer_accounts_after_commit(self):
        customer = structure_factories.CustomerFactory()
        account = factories.CustomerServiceAccountFactory(
            customer=customer, username="sa-customer"
        )
        self.mock_backend("sa-customer")

        with patch.object(
            tasks.close_service_accounts_task,
            "delay",
            side_effect=tasks.close_service_accounts_task,
        ) as delay:
            with self.captureOnCommitCallbacks(execute=True):
                customer.delete()

        self.assertEqual(delay.call_args.args[0], "CustomerServiceAccount")
        self.assertEqual(len(calls("PUT", "/sa-customer/close")), 1)
        self.assertFalse(
            models.CustomerServiceAccount.objects.filter(pk=account.pk).exists()
        )

    @patch("httpx.put")
    @patch("httpx.get")
    @patch("httpx.post")
    def test_service_account_calls_pass_explicit_timeout(
        self, mock_post, mock_get, mock_put
    ):
        account = self.make_account("sa-timeout")
        mock_post.return_value = httpx.Response(
            200,
            json={"access_token": "token"},
            request=httpx.Request("POST", TOKEN_URL),
        )
        mock_get.return_value = httpx.Response(
            200,
            json=service_account_response("sa-timeout"),
            request=httpx.Request("GET", SERVICE_ACCOUNT_URL),
        )
        mock_put.return_value = httpx.Response(
            200, json={}, request=httpx.Request("PUT", SERVICE_ACCOUNT_URL)
        )

        utils.close_service_account(account)
        utils.close_service_account_by_username("sa-other")
        utils.update_service_account(account)
        utils.rotate_service_account_api_key(account)

        for mock_call in mock_post.call_args_list + mock_get.call_args_list:
            self.assertEqual(
                mock_call.kwargs["timeout"], utils.ACCOUNT_API_REQUEST_TIMEOUT
            )
        for mock_call in mock_put.call_args_list:
            self.assertEqual(
                mock_call.kwargs["timeout"], utils.ACCOUNT_API_REQUEST_TIMEOUT
            )


@override_waldur_core_settings(
    COURSE_ACCOUNT_USE_API=True,
    COURSE_ACCOUNT_URL=COURSE_ACCOUNT_URL,
    COURSE_ACCOUNT_TOKEN_URL=TOKEN_URL,
    COURSE_ACCOUNT_TOKEN_CLIENT_ID="client-id",
    COURSE_ACCOUNT_TOKEN_SECRET="secret",
)
class CourseAccountAlreadyClosedTest(BaseClosingTest):
    def setUp(self):
        super().setUp()
        self.course_project = structure_factories.ProjectFactory(
            customer=self.fixture.customer, kind=ProjectKind.COURSE
        )
        self.user = structure_factories.UserFactory(username="course_00008")

    def mock_closed_at_backend(self):
        respx.get(f"{COURSE_ACCOUNT_URL}/course_00008").mock(
            return_value=httpx.Response(
                200, json=course_account_response("course_00008", "closed")
            )
        )
        respx.put(f"{COURSE_ACCOUNT_URL}/course_00008/close").mock(
            return_value=httpx.Response(
                400, json={"detail": "Temp account 'course_00008' is already closed"}
            )
        )

    def test_account_already_closed_at_backend_is_closed_locally(self):
        account = factories.CourseAccountFactory(
            project=self.course_project,
            user=self.user,
            state=CourseAccountState.ERRED,
            error_message="Temp account 'course_00008' is already closed",
        )
        self.mock_closed_at_backend()

        utils.close_course_account(account)

        self.assertEqual(calls("PUT"), [])
        account.refresh_from_db()
        self.assertEqual(account.state, CourseAccountState.CLOSED)
        self.assertEqual(account.error_message, "")
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

    def test_close_by_username_skips_account_closed_at_backend(self):
        self.mock_closed_at_backend()

        utils.close_course_account_by_username("course_00008")

        self.assertEqual(calls("PUT"), [])

    def test_username_outside_one_path_segment_is_never_sent(self):
        with self.assertRaises(utils.InvalidAccountUsername):
            utils.close_course_account_by_username("a/../b?c")

        self.assertEqual(calls("GET") + calls("PUT"), [])

    def test_token_failure_marks_only_accounts_with_a_backend_user(self):
        named = factories.CourseAccountFactory(
            project=self.course_project, user=self.user, state=CourseAccountState.OK
        )
        # The factory needs a user to build the account; clear it afterwards,
        # as for an account whose backend creation never returned one.
        never_created = factories.CourseAccountFactory(
            project=self.course_project, state=CourseAccountState.OK
        )
        models.CourseAccount.objects.filter(pk=never_created.pk).update(user=None)
        # The batch runs for a removed project only; a live one keeps its accounts.
        Project.objects.filter(pk=self.course_project.pk).update(is_removed=True)
        respx.clear()
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(401))

        tasks.close_course_accounts_task(
            [
                {"uuid": named.uuid.hex, "username": "course_00008", "user_id": None},
                {"uuid": never_created.uuid.hex, "username": None, "user_id": None},
            ]
        )

        named.refresh_from_db()
        self.assertEqual(named.state, CourseAccountState.ERRED)
        self.assertEqual(named.error_message, tasks.TOKEN_FAILURE_MESSAGE)
        never_created.refresh_from_db()
        self.assertEqual(never_created.state, CourseAccountState.CLOSED)


@override_waldur_core_settings(
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
class ReconcileRemovedProjectAccountsTest(BaseClosingTest):
    def setUp(self):
        super().setUp()
        self.removed = self.removed_project(kind=ProjectKind.COURSE)
        self.active = structure_factories.ProjectFactory(kind=ProjectKind.COURSE)
        # The sweep queues one task per project; run each inline.
        self.per_project = patch.object(
            tasks.reconcile_removed_project_accounts_for_project,
            "delay",
            side_effect=tasks.reconcile_removed_project_accounts_for_project,
        )
        self.queued = self.per_project.start()
        self.addCleanup(self.per_project.stop)

    def removed_project(self, removed_ago=timedelta(hours=2), **kwargs):
        # Removed without going through delete(), as if its own close had
        # failed or never been queued; long enough ago to be past the grace
        # period left to that close.
        project = structure_factories.ProjectFactory(**kwargs)
        Project.objects.filter(pk=project.pk).update(
            is_removed=True, modified=timezone.now() - removed_ago
        )
        return project

    def service_account(self, username, project=None, backend="active", **kwargs):
        if backend:
            respx.get(f"{SERVICE_ACCOUNT_URL}/{username}").mock(
                return_value=httpx.Response(
                    200, json=service_account_response(username, backend)
                )
            )
            respx.put(f"{SERVICE_ACCOUNT_URL}/{username}/close").mock(
                return_value=httpx.Response(kwargs.pop("close_status", 200), json={})
            )
        return factories.ProjectServiceAccountFactory(
            project=project or self.removed, username=username, **kwargs
        )

    def course_account(self, username, project=None, backend="active", **kwargs):
        if backend:
            respx.get(f"{COURSE_ACCOUNT_URL}/{username}").mock(
                return_value=httpx.Response(
                    200, json=course_account_response(username, backend)
                )
            )
            respx.put(f"{COURSE_ACCOUNT_URL}/{username}/close").mock(
                return_value=httpx.Response(200, json={})
            )
        user = structure_factories.UserFactory(username=username)
        return factories.CourseAccountFactory(
            project=project or self.removed, user=user, **kwargs
        )

    def test_closes_open_accounts_under_removed_projects(self):
        sa_open = self.service_account("sa-open")
        sa_erred = self.service_account(
            "sa-erred",
            backend="closed",
            state=ServiceAccountState.ERRED,
            error_message=tasks.TOKEN_FAILURE_MESSAGE,
        )
        ca_open = self.course_account("ca-open")
        ca_stuck = self.course_account(
            "ca-stuck",
            backend="closed",
            error_message="Temp account 'ca-stuck' is already closed",
        )

        tasks.reconcile_removed_project_accounts()

        self.assertEqual(
            sorted(str(call.request.url) for call in calls("PUT")),
            [
                f"{COURSE_ACCOUNT_URL}/ca-open/close",
                f"{SERVICE_ACCOUNT_URL}/sa-open/close",
            ],
        )
        # One token per account type, not per account.
        self.assertEqual(len(calls("POST")), 2)
        self.assertFalse(
            models.ProjectServiceAccount.objects.filter(
                pk__in=[sa_open.pk, sa_erred.pk]
            ).exists()
        )
        for account in (ca_open, ca_stuck):
            account.refresh_from_db()
            account.user.refresh_from_db()
            self.assertEqual(account.state, CourseAccountState.CLOSED)
            self.assertEqual(account.error_message, "")
            self.assertFalse(account.user.is_active)

    def test_leaves_other_accounts_alone(self):
        live_sa = self.service_account("sa-live", project=self.active, backend=None)
        live_ca = self.course_account("ca-live", project=self.active, backend=None)
        pending = self.course_account(
            "ca-pending", backend=None, state=CourseAccountState.PENDING
        )
        closed = self.course_account(
            "ca-closed", backend=None, state=CourseAccountState.CLOSED
        )

        tasks.reconcile_removed_project_accounts()

        self.assertEqual(respx.calls.call_count, 0)
        live_sa.refresh_from_db()
        self.assertEqual(live_sa.state, ServiceAccountState.OK)
        for account, state in (
            (live_ca, CourseAccountState.OK),
            (pending, CourseAccountState.PENDING),
            (closed, CourseAccountState.CLOSED),
        ):
            account.refresh_from_db()
            self.assertEqual(account.state, state)

    def test_nothing_to_reconcile_fetches_no_token(self):
        tasks.reconcile_removed_project_accounts()

        self.assertEqual(respx.calls.call_count, 0)

    def test_disabled_api_is_left_alone(self):
        account = self.service_account("sa-disabled", backend=None)

        with override_waldur_core_settings(
            SERVICE_ACCOUNT_USE_API=False, COURSE_ACCOUNT_USE_API=False
        ):
            tasks.reconcile_removed_project_accounts()

        self.assertEqual(respx.calls.call_count, 0)
        account.refresh_from_db()
        self.assertEqual(account.state, ServiceAccountState.OK)

    def test_failing_account_stays_erred_and_the_rest_close(self):
        failing = self.service_account("sa-failing", close_status=500)
        fine = self.service_account("sa-fine")

        with self.assertLogs("waldur_mastermind.marketplace.tasks", "INFO") as logs:
            tasks.reconcile_removed_project_accounts()

        failing.refresh_from_db()
        self.assertEqual(failing.state, ServiceAccountState.ERRED)
        self.assertFalse(
            models.ProjectServiceAccount.objects.filter(pk=fine.pk).exists()
        )
        self.assertTrue(
            any(
                "service" in line and "1 closed, 1 still open" in line
                for line in logs.output
            ),
            logs.output,
        )

    def test_is_scheduled_daily(self):
        entry = MarketplaceExtension.celery_tasks()[
            "reconcile_removed_project_accounts"
        ]
        self.assertEqual(
            entry["task"],
            "waldur_mastermind.marketplace.reconcile_removed_project_accounts",
        )
        self.assertEqual(entry["schedule"], crontab(minute=10, hour=3))

    def test_mock_only_course_backend_closes_without_a_token(self):
        account = self.course_account("ca-mock", backend=None)

        with (
            override_waldur_core_settings(
                COURSE_ACCOUNT_USE_API=False, COURSE_ACCOUNT_TOKEN_URL=""
            ),
            override_config(ENABLE_MOCK_COURSE_ACCOUNT_BACKEND=True),
        ):
            tasks.reconcile_removed_project_accounts()

        self.assertEqual(respx.calls.call_count, 0)
        account.refresh_from_db()
        self.assertEqual(account.state, CourseAccountState.CLOSED)

    def test_removed_projects_sharing_a_username_are_both_closed(self):
        # Neither scope is live, so neither row can own the account against
        # the other: closing it is what both need.
        closed_at_backend = []
        respx.get(f"{SERVICE_ACCOUNT_URL}/sa-dup").mock(
            side_effect=lambda request: httpx.Response(
                200,
                json=service_account_response(
                    "sa-dup", "closed" if closed_at_backend else "active"
                ),
            )
        )
        respx.put(f"{SERVICE_ACCOUNT_URL}/sa-dup/close").mock(
            side_effect=lambda request: (
                closed_at_backend.append(request) or httpx.Response(200, json={})
            )
        )
        other_removed = self.removed_project()
        first = self.service_account("sa-dup", backend=None)
        second = self.service_account("sa-dup", project=other_removed, backend=None)

        tasks.reconcile_removed_project_accounts()

        self.assertEqual(len(closed_at_backend), 1)
        self.assertFalse(
            models.ProjectServiceAccount.objects.filter(
                pk__in=[first.pk, second.pk]
            ).exists()
        )

    def test_a_live_account_sharing_the_username_still_blocks_the_close(self):
        removed = self.service_account("sa-shared")
        live = self.service_account("sa-shared", project=self.active, backend=None)

        tasks.reconcile_removed_project_accounts()

        self.assertEqual(calls("PUT"), [])
        removed.refresh_from_db()
        self.assertEqual(removed.state, ServiceAccountState.ERRED)
        live.refresh_from_db()
        self.assertEqual(live.state, ServiceAccountState.OK)

    def test_an_invalid_username_is_closed_in_waldur_only_and_kept(self):
        account = self.service_account("../x", backend=None)

        with self.assertLogs("waldur_mastermind.marketplace.tasks", "INFO") as logs:
            tasks.reconcile_removed_project_accounts()

        # Nothing to ask the backend, so not even a token is fetched.
        self.assertEqual(respx.calls.call_count, 0)
        # Its backend account may still be live, so the row stays for staff.
        account.refresh_from_db()
        self.assertEqual(account.state, ServiceAccountState.CLOSED)
        self.assertTrue(account.error_message.startswith(utils.CLOSED_IN_WALDUR_ONLY))
        self.assertTrue(
            any("1 closed in Waldur only" in line for line in logs.output),
            logs.output,
        )

    def test_token_failure_still_closes_invalid_usernames_in_waldur_only(self):
        service_account = self.service_account("../x", backend=None)
        course_account = self.course_account("bad name", backend=None)
        respx.clear()
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(500))

        tasks.reconcile_removed_project_accounts()

        self.assertEqual(calls("POST"), [])
        for account in (service_account, course_account):
            account.refresh_from_db()
            self.assertTrue(
                account.error_message.startswith(utils.CLOSED_IN_WALDUR_ONLY),
                account.error_message,
            )

    def test_a_course_user_shared_with_a_live_account_is_left_open(self):
        stale = self.course_account("ca-shared", state=CourseAccountState.ERRED)
        live = factories.CourseAccountFactory(project=self.active, user=stale.user)

        tasks.reconcile_removed_project_accounts()

        self.assertEqual(calls("PUT"), [])
        stale.refresh_from_db()
        self.assertEqual(stale.state, CourseAccountState.CLOSED)
        self.assertTrue(stale.error_message.startswith(utils.CLOSED_IN_WALDUR_ONLY))
        stale.user.refresh_from_db()
        self.assertTrue(stale.user.is_active)
        live.refresh_from_db()
        self.assertEqual(live.state, CourseAccountState.OK)

    def test_queues_one_task_per_project_each_with_its_own_token(self):
        other_removed = self.removed_project()
        self.service_account("sa-first")
        self.service_account("sa-second", project=other_removed)

        tasks.reconcile_removed_project_accounts()

        self.assertCountEqual(
            [call.args[0] for call in self.queued.call_args_list],
            [self.removed.pk, other_removed.pk],
        )
        # A token per project, so a long first run never outlives one.
        self.assertEqual(len(calls("POST")), 2)
        self.assertEqual(len(calls("PUT", "/close")), 2)

    def test_a_project_removed_within_the_grace_period_is_left_to_its_own_close(
        self,
    ):
        just_removed = self.removed_project(removed_ago=timedelta(minutes=5))
        account = self.service_account("sa-recent", project=just_removed, backend=None)

        tasks.reconcile_removed_project_accounts()

        self.queued.assert_not_called()
        self.assertEqual(respx.calls.call_count, 0)
        account.refresh_from_db()
        self.assertEqual(account.state, ServiceAccountState.OK)

    def test_a_project_recovered_after_the_sweep_keeps_its_accounts(self):
        account = self.service_account("sa-recovered", backend=None)
        Project.objects.filter(pk=self.removed.pk).update(is_removed=False)

        tasks.reconcile_removed_project_accounts_for_project(self.removed.pk)

        self.assertEqual(respx.calls.call_count, 0)
        account.refresh_from_db()
        self.assertEqual(account.state, ServiceAccountState.OK)

    def test_a_project_recovered_during_the_close_keeps_the_rest(self):
        # The batch checks each row's project again, so a recovery between
        # the payload being taken and the row being reached stops the close.
        service_account = self.service_account("sa-late", backend=None)
        course_account = self.course_account("ca-late", backend=None)
        payloads = (
            utils.service_account_close_payload(
                models.ProjectServiceAccount.objects.filter(pk=service_account.pk)
            ),
            utils.course_account_close_payload(
                models.CourseAccount.objects.filter(pk=course_account.pk)
            ),
        )
        Project.objects.filter(pk=self.removed.pk).update(is_removed=False)

        tasks.close_service_accounts_task("ProjectServiceAccount", payloads[0])
        tasks.close_course_accounts_task(payloads[1])

        self.assertEqual(calls("PUT"), [])
        service_account.refresh_from_db()
        self.assertEqual(service_account.state, ServiceAccountState.OK)
        course_account.refresh_from_db()
        self.assertEqual(course_account.state, CourseAccountState.OK)

    def test_a_stale_pending_course_account_is_closed(self):
        # PENDING a day later means no close is in flight: say the broker was
        # down when a destroy queued it.
        stale = self.course_account("ca-stale", state=CourseAccountState.PENDING)
        models.CourseAccount.objects.filter(pk=stale.pk).update(
            modified=timezone.now() - timedelta(days=2)
        )
        fresh = self.course_account(
            "ca-fresh", backend=None, state=CourseAccountState.PENDING
        )

        tasks.reconcile_removed_project_accounts()

        self.assertEqual(
            [str(call.request.url) for call in calls("PUT")],
            [f"{COURSE_ACCOUNT_URL}/ca-stale/close"],
        )
        stale.refresh_from_db()
        self.assertEqual(stale.state, CourseAccountState.CLOSED)
        fresh.refresh_from_db()
        self.assertEqual(fresh.state, CourseAccountState.PENDING)

    def test_a_soft_time_limit_ends_the_task_instead_of_marking_the_account(self):
        account = self.service_account("sa-slow", backend=None)

        with (
            patch.object(
                utils, "close_service_account", side_effect=SoftTimeLimitExceeded
            ),
            self.assertRaises(SoftTimeLimitExceeded),
        ):
            tasks.reconcile_removed_project_accounts_for_project(self.removed.pk)

        # Left as it was, for the next night's run.
        account.refresh_from_db()
        self.assertEqual(account.state, ServiceAccountState.OK)


@override_waldur_core_settings(
    COURSE_ACCOUNT_USE_API=True,
    COURSE_ACCOUNT_URL=COURSE_ACCOUNT_URL,
    COURSE_ACCOUNT_TOKEN_URL=TOKEN_URL,
    COURSE_ACCOUNT_TOKEN_CLIENT_ID="client-id",
    COURSE_ACCOUNT_TOKEN_SECRET="secret",
)
class CourseAccountClosedByUsernameTest(BaseClosingTest):
    """The batch's path for rows a hard delete already removed."""

    def close(self, user):
        tasks.close_course_accounts_task(
            [{"uuid": uuid.uuid4().hex, "username": user.username, "user_id": user.pk}]
        )

    def test_an_invalid_username_still_deactivates_the_user(self):
        user = structure_factories.UserFactory(username="bad name")

        self.close(user)

        self.assertEqual(calls("GET") + calls("PUT"), [])
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_a_user_shared_with_a_live_account_is_left_open(self):
        respx.get(f"{COURSE_ACCOUNT_URL}/ca-shared").mock(
            return_value=httpx.Response(200, json=course_account_response("ca-shared"))
        )
        respx.put(f"{COURSE_ACCOUNT_URL}/ca-shared/close").mock(
            return_value=httpx.Response(200, json={})
        )
        user = structure_factories.UserFactory(username="ca-shared")
        live = factories.CourseAccountFactory(
            project=structure_factories.ProjectFactory(kind=ProjectKind.COURSE),
            user=user,
        )

        self.close(user)

        self.assertEqual(calls("GET") + calls("PUT"), [])
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        live.refresh_from_db()
        self.assertEqual(live.state, CourseAccountState.OK)
