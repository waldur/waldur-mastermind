import uuid
from typing import cast
from unittest import mock

from django.test import override_settings
from rest_framework import status, test

from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_rancher.const import OS_LB_PREFIX
from waldur_openstack.tests import factories as os_factories
from waldur_rancher import models as rancher_models
from waldur_rancher.tests import fixtures


class ManagedRancherClusterIPTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.RancherFixture()
        self.neutron_client_patcher = mock.patch(
            "waldur_openstack.backend.get_neutron_client"
        )
        self.mock_neutron_client = self.neutron_client_patcher.start()
        self.mock_neutron_client().create_floatingip.return_value = {
            "floatingip": {
                "status": "online",
                "floating_ip_address": "0.0.0.0",
                "id": 1,
                "floating_network_id": 1,
            }
        }

        self.mock_neutron_client().list_floatingips.side_effect = [
            {"floatingips": []},
            {
                "floatingips": [
                    {
                        "id": 1,
                    }
                ]
            },
        ]

        self.mock_neutron_client().update_floatingip.return_value = {}

        self.mock_neutron_client().show_floatingip.return_value = {
            "floatingip": {
                "status": "ACTIVE",
            }
        }

        self.keystone_session_patcher = mock.patch(
            "waldur_openstack.backend.get_keystone_session"
        )
        self.mock_keystone_session = self.keystone_session_patcher.start()

        self.cluster = self.fixture.cluster
        self.instance = self.fixture.instance
        self.tenant = self.fixture.tenant
        service_settings = self.tenant.service_settings
        options = cast(dict, service_settings.options)
        options["external_network_id"] = uuid.uuid4().hex
        service_settings.save()

        self.resource = marketplace_factories.ResourceFactory(
            scope=self.cluster,
            state=ResourceStates.OK,
        )

        network = os_factories.NetworkFactory(
            service_settings=service_settings,
            project=self.fixture.project,
            tenant=self.tenant,
        )
        self.subnet = os_factories.SubNetFactory(
            service_settings=service_settings,
            network=network,
            tenant=self.tenant,
        )

        self.port = os_factories.PortFactory(
            instance=self.instance,
            tenant=self.tenant,
            subnet=self.subnet,
            service_settings=service_settings,
        )

        self.instance.name = f"{OS_LB_PREFIX}{self.resource.slug}"
        self.instance.ports.add(self.port)
        self.instance.subnets.add(self.subnet)
        self.instance.save()

        self.url = os_factories.InstanceFactory.get_url(
            self.instance, "update_floating_ips"
        )

    def tearDown(self):
        super().tearDown()
        self.neutron_client_patcher.stop()
        self.keystone_session_patcher.stop()

    @override_settings(task_always_eager=True)
    def test_public_ip_is_created_for_cluster(self):
        self.client.force_login(self.fixture.staff)  # type: ignore

        payload = {
            "floating_ips": [
                {"subnet": os_factories.SubNetFactory.get_url(self.subnet)}
            ]
        }
        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        floating_ip = self.instance.floating_ips.first()
        self.assertTrue(
            rancher_models.ClusterPublicIP.objects.filter(
                cluster=self.cluster,
                floating_ip=floating_ip,
            ).exists()
        )
