from __future__ import unicode_literals

import os
import re
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import validators
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel

from waldur_ansible.common import models as common_models
from waldur_core.core import models as core_models, fields as core_fields
from waldur_openstack.openstack_tenant import models as openstack_models

from .backend import AnsiblePlaybookBackend

User = get_user_model()


def get_upload_path(instance, filename):
    return '%s/%s.png' % (instance._meta.model_name, instance.uuid.hex)


@python_2_unicode_compatible
class Playbook(core_models.UuidMixin,
               core_models.NameMixin,
               core_models.DescribableMixin,
               models.Model):
    workspace = models.CharField(max_length=255, unique=True, help_text=_('Absolute path to the playbook workspace.'))
    entrypoint = models.CharField(max_length=255, help_text=_('Relative path to the file in the workspace to execute.'))
    image = models.ImageField(upload_to=get_upload_path, null=True, blank=True)
    tracker = FieldTracker()

    @staticmethod
    def get_url_name():
        return 'ansible_playbook'

    def get_playbook_path(self):
        return os.path.join(self.workspace, self.entrypoint)

    @staticmethod
    def generate_workspace_path():
        base_path = os.path.join(
            settings.MEDIA_ROOT,
            settings.WALDUR_PLAYBOOK_JOBS.get('PLAYBOOKS_DIR_NAME', 'ansible_playbooks'),
        )
        path = os.path.join(base_path, uuid.uuid4().hex)
        while os.path.exists(path):
            path = os.path.join(base_path, uuid.uuid4().hex)

        return path

    def get_backend(self):
        return AnsiblePlaybookBackend(self)

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class PlaybookParameter(core_models.DescribableMixin, models.Model):
    class Meta(object):
        unique_together = ('playbook', 'name')
        ordering = ['order']

    name = models.CharField(
        max_length=255,
        validators=[validators.RegexValidator(re.compile('^[\w]+$'), _('Enter a valid name.'))],
        help_text=_('Required. 255 characters or fewer. Letters, numbers and _ characters'),
    )
    playbook = models.ForeignKey(Playbook, on_delete=models.CASCADE, related_name='parameters')
    required = models.BooleanField(default=False)
    default = models.CharField(max_length=255, blank=True, help_text=_('Default argument for this parameter.'))
    order = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class Job(core_models.UuidMixin,
          core_models.StateMixin,
          core_models.NameMixin,
          core_models.DescribableMixin,
          TimeStampedModel,
          common_models.ApplicationModel):
    class Meta(object):
        pass

    class Permissions(object):
        project_path = 'service_project_link__project'
        customer_path = 'service_project_link__project__customer'

    user = models.ForeignKey(User, related_name='+')
    ssh_public_key = models.ForeignKey(core_models.SshPublicKey, related_name='+')
    service_project_link = models.ForeignKey(openstack_models.OpenStackTenantServiceProjectLink, related_name='+')
    subnet = models.ForeignKey(openstack_models.SubNet, related_name='+')
    playbook = models.ForeignKey(Playbook, related_name='jobs')
    arguments = core_fields.JSONField(default=dict, blank=True, null=True)
    output = models.TextField(blank=True)

    @staticmethod
    def get_url_name():
        return 'ansible_job'

    def get_backend(self):
        return self.playbook.get_backend()

    def __str__(self):
        return self.name

    def get_tag(self):
        return 'job:%s' % self.uuid.hex

    def get_related_resources(self):
        return openstack_models.Instance.objects.filter(tags__name=self.get_tag())
