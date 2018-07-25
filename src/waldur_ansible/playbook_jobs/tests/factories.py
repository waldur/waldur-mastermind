from __future__ import unicode_literals

import factory
from rest_framework.reverse import reverse
from waldur_ansible.playbook_jobs import models
from waldur_core.core.utils import get_detail_view_name, get_list_view_name
from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack.openstack_tenant.tests import factories as openstack_factories


class PlaybookFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Playbook

    name = factory.Sequence(lambda n: 'playbook%s' % n)
    description = factory.Sequence(lambda n: 'Description %s' % n)
    workspace = factory.Sequence(lambda n: '/path/to/workspace%s' % n)
    entrypoint = 'main.yml'

    @factory.post_generation
    def parameters(self, create, extracted, **kwargs):
        if not create:
            return

        if extracted:
            for parameter in extracted:
                self.parameters.add(parameter)
        else:
            PlaybookParameterFactory.create_batch(3, playbook=self)

    @classmethod
    def get_url(cls, playbook=None, action=None):
        if playbook is None:
            playbook = PlaybookFactory()

        url = 'http://testserver' + reverse(get_detail_view_name(models.Playbook), kwargs={'uuid': playbook.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse(get_list_view_name(models.Playbook))


class PlaybookParameterFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.PlaybookParameter

    playbook = factory.SubFactory(PlaybookFactory)
    name = factory.Sequence(lambda n: 'parameter%s' % n)
    description = factory.Sequence(lambda n: 'Description %s' % n)
    default = factory.Sequence(lambda n: 'Value%s' % n)


class JobFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Job

    state = models.Job.States.OK
    name = factory.Sequence(lambda n: 'job%s' % n)
    description = factory.Sequence(lambda n: 'Description %s' % n)
    playbook = factory.SubFactory(PlaybookFactory)
    service_project_link = factory.SubFactory(openstack_factories.OpenStackTenantServiceProjectLinkFactory)
    ssh_public_key = factory.SubFactory(structure_factories.SshPublicKeyFactory)
    subnet = factory.SubFactory(openstack_factories.SubNetFactory)
    user = factory.SubFactory(structure_factories.UserFactory)

    @factory.post_generation
    def arguments(self, create, extracted, **kwargs):
        if not create:
            return

        if extracted:
            self.arguments = extracted
        else:
            self.arguments = {name: 'test value' for name in
                              self.playbook.parameters.all().values_list('name', flat=True)}
        self.save(update_fields=['arguments'])

    @classmethod
    def get_url(cls, job=None, action=None):
        if job is None:
            job = JobFactory()

        url = 'http://testserver' + reverse(get_detail_view_name(models.Job), kwargs={'uuid': job.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse(get_list_view_name(models.Job))
