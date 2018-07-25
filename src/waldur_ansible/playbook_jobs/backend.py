import json
import logging
import os
import subprocess  # nosec

import six
from django.conf import settings

from waldur_ansible.common.exceptions import AnsibleBackendError
from waldur_core.core.views import RefreshTokenMixin

logger = logging.getLogger(__name__)


class AnsiblePlaybookBackend(object):
    def __init__(self, playbook):
        self.playbook = playbook

    def _get_command(self, job, check_mode):
        playbook_path = self.playbook.get_playbook_path()
        if not os.path.exists(playbook_path):
            raise AnsibleBackendError('Playbook %s does not exist.' % playbook_path)

        command = [settings.WALDUR_ANSIBLE_COMMON.get('PLAYBOOK_EXECUTION_COMMAND', 'ansible-playbook')]
        if settings.WALDUR_ANSIBLE_COMMON.get('PLAYBOOK_ARGUMENTS'):
            command.extend(settings.WALDUR_ANSIBLE_COMMON.get('PLAYBOOK_ARGUMENTS'))

        if check_mode:
            command.append('--check')

        extra_vars = job.arguments.copy()
        extra_vars.update(self._get_extra_vars(job))
        # XXX: Passing arguments in following way is supported in Ansible>=1.2
        command.extend(['--extra-vars', json.dumps(extra_vars)])

        command.extend(['--ssh-common-args', '-o UserKnownHostsFile=/dev/null'])
        return command + [playbook_path]

    def _get_extra_vars(self, job):
        return dict(
            api_url=settings.WALDUR_ANSIBLE_COMMON['API_URL'],
            access_token=RefreshTokenMixin().refresh_token(job.user).key,
            project_uuid=job.service_project_link.project.uuid.hex,
            provider_uuid=job.service_project_link.service.uuid.hex,
            private_key_path=settings.WALDUR_ANSIBLE_COMMON['PRIVATE_KEY_PATH'],
            public_key_uuid=settings.WALDUR_ANSIBLE_COMMON['PUBLIC_KEY_UUID'],
            user_key_uuid=job.ssh_public_key.uuid.hex,
            subnet_uuid=job.subnet.uuid.hex,
            tags=[job.get_tag()],
        )

    def run_job(self, job, check_mode=False):
        command = self._get_command(job, check_mode)
        command_str = ' '.join(command)

        logger.debug('Executing command "%s".', command_str)
        env = dict(
            os.environ,
            ANSIBLE_LIBRARY=settings.WALDUR_ANSIBLE_COMMON['ANSIBLE_LIBRARY'],
            ANSIBLE_HOST_KEY_CHECKING='False',
        )
        try:
            output = subprocess.check_output(command, stderr=subprocess.STDOUT, env=env)  # nosec
        except subprocess.CalledProcessError as e:
            logger.info('Failed to execute command "%s".', command_str)
            job.output = e.output
            job.save(update_fields=['output'])
            six.reraise(AnsibleBackendError, e)
        else:
            logger.info('Command "%s" was successfully executed.', command_str)
            job.output = output
            job.save(update_fields=['output'])

    def decode_output(self, output):
        items = []
        for line in output.splitlines():
            if 'WALDUR_CHECK_MODE' not in line:
                continue
            parts = line.split(' => ')
            if len(parts) != 2:
                continue
            try:
                payload = json.loads(parts[1])
            except TypeError:
                continue
            if 'instance' in payload:
                payload = payload['instance']
            if 'WALDUR_CHECK_MODE' in payload:
                del payload['WALDUR_CHECK_MODE']
            if payload:
                items.append(payload)
        return items
