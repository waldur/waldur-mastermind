from ddt import ddt
from rest_framework import test

from waldur_core.structure.exceptions import ServiceBackendNotImplemented
from waldur_core.structure.tasks import ServiceResourcesPullTask
from waldur_mastermind.marketplace_slurm_remote import PLUGIN_NAME
from waldur_slurm.tests import factories as slurm_factories


@ddt
class TestSetAllocationState(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.service_settings = slurm_factories.SlurmServiceSettingsFactory(
            type=PLUGIN_NAME
        )

    def test_task_does_not_raise_exception_if_settings_backend_does_not_exist(self):
        try:
            ServiceResourcesPullTask().pull(self.service_settings)
        except ServiceBackendNotImplemented:
            self.fail(
                'ServiceResourcesPullTask does not work for %s settings.' % PLUGIN_NAME
            )
