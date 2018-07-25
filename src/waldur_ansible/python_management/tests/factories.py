from __future__ import unicode_literals

import factory
from rest_framework.reverse import reverse
from waldur_ansible.python_management import models
from waldur_openstack.openstack_tenant.tests import factories as openstack_factories

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories


class PythonManagementFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.PythonManagement

    user = factory.SubFactory(structure_factories.UserFactory)
    instance = factory.SubFactory(openstack_factories.InstanceFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
    virtual_envs_dir_path = factory.Sequence(lambda n: 'virtual_envs_dir_path%s' % n)

    @classmethod
    def get_url(cls, python_management=None, action=None):
        if python_management is None:
            python_management = PythonManagementFactory()

        url = 'http://testserver' + reverse(core_utils.get_detail_view_name(models.PythonManagement), kwargs={'uuid': python_management.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse(core_utils.get_list_view_name(models.PythonManagement))


class PythonManagementInitializeRequestFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.PythonManagementInitializeRequest

    python_management = factory.SubFactory(PythonManagementFactory)
    output = factory.Sequence(lambda n: n)


class PythonManagementSynchronizeRequestFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.PythonManagementSynchronizeRequest

    python_management = factory.SubFactory(PythonManagementFactory)
    initialization_request = factory.SubFactory(PythonManagementInitializeRequestFactory)
    libraries_to_install = factory.Sequence(lambda n: n)
    libraries_to_remove = factory.Sequence(lambda n: n)
    output = factory.Sequence(lambda n: n)
    virtual_env_name = factory.Sequence(lambda n: n)


class VirtualEnvironmentFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.VirtualEnvironment

    name = factory.Sequence(lambda n: 'name%s' % n)
    python_management = factory.SubFactory(PythonManagementFactory)


class InstalledLibraryFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.InstalledLibrary

    virtual_environment = factory.SubFactory(VirtualEnvironmentFactory)
    version = factory.Sequence(lambda n: 'version%s' % n)
