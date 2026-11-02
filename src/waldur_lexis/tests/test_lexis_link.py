import responses
from ddt import data, ddt
from django.test import override_settings
from django.urls import reverse
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ServiceProviderRole
from waldur_lexis import models, serializers
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import RobotAccountStates
from waldur_mastermind.marketplace.tests import factories, fixtures


class LexisLinkCreateTest(test.APITestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.set_state_ok()
        self.resource.backend_id = "project_12345"
        self.resource.save()

        self.ssh_key = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQDRmKSYeNxfyNGIoYqQCXUjLlMFJSCX/Jx+k0ODlg0xpMMlBEEK test"

        offering = self.resource.offering
        offering.plugin_options.update(
            {
                "heappe_url": "https://heappy.example.com",
                "heappe_username": "heappe_user",
                "heappe_cluster_id": 1,
                "heappe_local_base_path": "~/",
                "heappe_identifier": "example-cluster",
                "scratch_project_directory": "/scratch/projects",
                "project_permanent_directory": "/permanent/projects",
            }
        )
        offering.secret_options.update(
            {
                "heappe_password": "6d9384da6e19449b9312a6c08b07f1e0",
                "heappe_cluster_password": "pass",
            }
        )
        offering.save()
        responses.start()

        responses.add(
            responses.POST,
            "https://heappy.example.com/heappe/UserAndLimitationManagement/AuthenticateUserPassword",
            json="557bf7e928b64fd0bcc41579b5888967",
        )

        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.DELETE_LEXIS_LINK)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.CREATE_LEXIS_LINK)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_LEXIS_LINK)

    def tearDown(self):
        super().tearDown()
        responses.stop()
        responses.reset()

    def test_robot_account_created_upon_lexis_link_creation(self):
        self.client.force_login(self.fixture.service_owner)
        url = "http://testserver" + reverse("lexis-link-list")
        self.assertEqual(
            0,
            marketplace_models.RobotAccount.objects.filter(
                type__istartswith="hl", resource=self.resource
            ).count(),
        )
        response = self.client.post(
            url, data={"resource": factories.ResourceFactory.get_url(self.resource)}
        )

        self.assertEqual(201, response.status_code, response.data)
        self.assertEqual(
            1,
            marketplace_models.RobotAccount.objects.filter(
                type__istartswith="hl", resource=self.resource
            ).count(),
        )
        robot_account = marketplace_models.RobotAccount.objects.get(
            type__istartswith="hl", resource=self.resource
        )
        self.assertEqual("", robot_account.username)
        self.assertEqual("hl000", robot_account.type)

    @override_settings(task_always_eager=True)
    def test_robot_account_username_update_triggers_task(self):
        responses.add(
            responses.POST,
            "https://heappy.example.com/heappe/Management/SecureShellKey",
            json=[{"PublicKeyOpenSSH": self.ssh_key}],
        )

        responses.add(
            responses.GET,
            "https://heappy.example.com/heappe/UserAndLimitationManagement/ProjectsForCurrentUser",
            json=[],
        )

        responses.add(
            responses.GET,
            "https://heappy.example.com/heappe/ClusterInformation/ListAvailableClusters",
            json=[{"Id": 1}],
        )

        responses.add(
            responses.POST,
            "https://heappy.example.com/heappe/Management/Project",
            json={"Id": 1},
        )

        responses.add(
            responses.POST,
            "https://heappy.example.com/heappe/Management/ProjectAssignmentToCluster",
            json={},
        )
        responses.add(
            responses.GET,
            "https://heappy.example.com/heappe/Management/TestClusterAccessForAccount",
            json="accessible with selected account",
        )
        responses.add(
            responses.POST,
            "https://heappy.example.com/heappe/Management/InitializeClusterScriptDirectory",
            json={},
        )

        robot_account = marketplace_models.RobotAccount.objects.create(
            username="",
            type="hl001",
            resource=self.resource,
        )

        # Set up proper state transitions
        robot_account.begin_creating()
        robot_account.save()

        lexis_link = models.LexisLink.objects.create(robot_account=robot_account)

        robot_account.username = "test_username"
        robot_account.set_ok()
        robot_account.save()
        robot_account.refresh_from_db()
        lexis_link.refresh_from_db()
        self.assertEqual(1, len(robot_account.keys))
        self.assertEqual(self.ssh_key, robot_account.keys[0])
        self.assertEqual(models.LexisLink.States.OK, lexis_link.state)
        # The HEAppE project is the backend's to record, not the API caller's.
        self.assertEqual(1, lexis_link.heappe_project_id)

    def link_to_heappe_project(self, heappe_project_id):
        robot_account = marketplace_models.RobotAccount.objects.create(
            username="test_username",
            type="hl001",
            resource=self.resource,
            state=RobotAccountStates.OK,
        )
        return models.LexisLink.objects.create(
            robot_account=robot_account, heappe_project_id=heappe_project_id
        )

    def mock_heappe_projects(self):
        responses.add(
            responses.GET,
            "https://heappy.example.com/heappe/UserAndLimitationManagement/ProjectsForCurrentUser",
            json=[
                {
                    "Project": {
                        "Id": 1,
                        "Name": self.resource.name,
                        "AccountingString": self.resource.backend_id,
                    }
                },
                {
                    "Project": {
                        "Id": 2,
                        "Name": "another resource",
                        "AccountingString": "project_67890",
                    }
                },
            ],
        )
        responses.add(
            responses.DELETE,
            "https://heappy.example.com/heappe/Management/Project",
            json="",
        )

    def heappe_project_deletions(self):
        return [
            call
            for call in responses.calls
            if call.request.method == "DELETE"
            and call.request.url.endswith("/heappe/Management/Project")
        ]

    def test_heappe_project_of_the_links_resource_is_deleted(self):
        self.mock_heappe_projects()
        lexis_link = self.link_to_heappe_project(1)

        lexis_link.get_backend().delete_heappe_project(lexis_link)

        self.assertEqual(1, len(self.heappe_project_deletions()))

    def test_heappe_project_of_another_resource_is_not_deleted(self):
        # The id could be changed through the API until that was closed, so a
        # stored id may name a project that is not this resource's.
        self.mock_heappe_projects()
        lexis_link = self.link_to_heappe_project(2)

        lexis_link.get_backend().delete_heappe_project(lexis_link)

        self.assertEqual([], self.heappe_project_deletions())

    @override_settings(task_always_eager=True)
    def test_lexis_link_deletion_triggers_ssh_key_revoke(self):
        responses.add(
            responses.DELETE,
            "https://heappy.example.com/heappe/Management/SecureShellKey",
            json="",
        )

        responses.add(
            responses.GET,
            "https://heappy.example.com/heappe/UserAndLimitationManagement/ProjectsForCurrentUser",
            json=[
                {
                    "Project": {
                        "Id": 1,
                        "Name": self.resource.name,
                        "AccountingString": self.resource.backend_id,
                    }
                }
            ],
        )

        responses.add(
            responses.DELETE,
            "https://heappy.example.com/heappe/Management/Project",
            json="",
        )

        robot_account = marketplace_models.RobotAccount.objects.create(
            username="test_username",
            type="hl001",
            resource=self.resource,
            keys=[self.ssh_key],
            state=RobotAccountStates.OK,
        )

        lexis_link = models.LexisLink.objects.create(
            robot_account=robot_account, state=models.LexisLink.States.OK
        )
        url = "http://testserver" + reverse(
            "lexis-link-detail", kwargs={"uuid": lexis_link.uuid.hex}
        )
        self.client.force_login(self.fixture.service_manager)
        response = self.client.delete(url)
        self.assertEqual(204, response.status_code, url)

        self.assertIsNone(
            models.LexisLink.objects.filter(uuid=lexis_link.uuid.hex).first()
        )
        robot_account.refresh_from_db()
        # Simulate 3rd party request to delete robot account
        robot_account.set_deleted()
        robot_account.save()
        self.assertEqual(
            RobotAccountStates.DELETED,
            robot_account.state,
        )

    def test_get_heappe_config_includes_new_optional_fields(self):
        """Test that get_heappe_config properly handles the new optional fields"""
        config = models.get_heappe_config(self.resource.offering)

        self.assertEqual(config.scratch_project_directory, "/scratch/projects")
        self.assertEqual(config.project_permanent_directory, "/permanent/projects")
        self.assertEqual(config.heappe_url, "https://heappy.example.com")
        self.assertEqual(config.heappe_username, "heappe_user")
        self.assertEqual(config.heappe_cluster_id, 1)
        self.assertEqual(config.heappe_local_base_path, "~/")
        self.assertEqual(config.heappe_identifier, "example-cluster")


@ddt
class LexisLinkUpdateTest(test.APITestCase):
    """The HEAppE project a link points at is set by the backend alone.

    Waldur uses it for every HEAppE project call it makes for the link, so
    whoever could change it would redirect those calls to another project.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        robot_account = factories.RobotAccountFactory(
            resource=self.fixture.resource, type="hl000", username="robot"
        )
        self.lexis_link = models.LexisLink.objects.create(
            robot_account=robot_account, heappe_project_id=1
        )
        self.url = reverse(
            "lexis-link-detail", kwargs={"uuid": self.lexis_link.uuid.hex}
        )

    @data(
        "staff",
        "owner",
        "admin",
        "manager",
        "member",
        "customer_support",
        "global_support",
        "user",
    )
    def test_nobody_can_change_the_heappe_project(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        for method in ("put", "patch"):
            response = getattr(self.client, method)(
                self.url, {"heappe_project_id": 999}, format="json"
            )
            self.assertEqual(
                response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED, method
            )

        self.lexis_link.refresh_from_db()
        self.assertEqual(self.lexis_link.heappe_project_id, 1)

    def test_heappe_project_stays_read_only_if_updates_return(self):
        field = serializers.LexisLinkSerializer().fields["heappe_project_id"]
        self.assertTrue(field.read_only)
