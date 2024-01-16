import responses
from django.test import override_settings
from django.urls import reverse
from rest_framework import test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_lexis import models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories, fixtures


class LexisLinkCreateTest(test.APITransactionTestCase):
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

        CustomerRole.MANAGER.add_permission(PermissionEnum.DELETE_LEXIS_LINK)

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
            json={"PublicKeyOpenSSH": self.ssh_key},
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

        robot_account = marketplace_models.RobotAccount.objects.create(
            username="",
            type="hl001",
            resource=self.resource,
        )
        lexis_link = models.LexisLink.objects.create(robot_account=robot_account)

        robot_account.username = "test_username"
        robot_account.save()
        robot_account.refresh_from_db()
        lexis_link.refresh_from_db()
        self.assertEqual(1, len(robot_account.keys))
        self.assertEqual(self.ssh_key, robot_account.keys[0])
        self.assertEqual(models.LexisLink.States.OK, lexis_link.state)

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
        self.assertIsNone(
            marketplace_models.RobotAccount.objects.filter(
                uuid=robot_account.uuid.hex
            ).first()
        )
