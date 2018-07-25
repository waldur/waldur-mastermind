import json
import logging
import os
import subprocess  # nosec

import six
from django.conf import settings

from waldur_ansible.common import exceptions
from waldur_core.core.views import RefreshTokenMixin

from . import utils

logger = logging.getLogger(__name__)


class ManagementRequestsBackend(object):

    def is_processing_allowed(self, request):
        raise NotImplementedError

    def build_locked_for_processing_message(self, request):
        raise NotImplementedError

    def lock_for_processing(self, request):
        raise NotImplementedError

    def handle_on_processing_finished(self, request):
        raise NotImplementedError

    def get_playbook_path(self, request):
        raise NotImplementedError

    def get_user(self, request):
        raise NotImplementedError

    def build_additional_extra_vars(self, request):
        raise NotImplementedError

    def instantiate_line_post_processor_class(self, request):
        raise NotImplementedError

    def instantiate_extracted_information_handler_class(self, request):
        raise NotImplementedError

    def instantiate_error_handler_class(self, request):
        raise NotImplementedError

    def process_request(self, request):
        if not self.is_processing_allowed(request):
            request.output = self.build_locked_for_processing_message(request)
            request.save(update_fields=['output'])
            raise exceptions.LockedForProcessingError('Could not process request %s ' % request)
        try:
            self.lock_for_processing(request)

            command = self.build_command(request)
            command_str = ' '.join(command)

            logger.debug('Executing command "%s".', command_str)
            env = dict(
                os.environ,
                ANSIBLE_LIBRARY=settings.WALDUR_ANSIBLE_COMMON['ANSIBLE_LIBRARY'],
                ANSIBLE_HOST_KEY_CHECKING='False',
                ANSIBLE_RETRY_FILES_ENABLED='False',
                ANSIBLE_REMOTE_PORT=settings.WALDUR_ANSIBLE_COMMON['REMOTE_VM_SSH_PORT'],
            )
            lines_post_processor_instance = self.instantiate_line_post_processor_class(request)
            extracted_information_handler = self.instantiate_extracted_information_handler_class(request)
            error_handler = self.instantiate_error_handler_class(request)
            try:
                for output_line in utils.subprocess_output_iterator(command, env):
                    request.output += output_line
                    request.save(update_fields=['output'])
                    lines_post_processor_instance.post_process_line(output_line)
            except subprocess.CalledProcessError as e:
                logger.error('%s - failed to execute command "%s".', request, command_str)
                logger.error('%s - Ansible request processing output: \n %s.', request, request.output)
                error_handler.handle_error(request, lines_post_processor_instance)
                six.reraise(exceptions.AnsibleBackendError, e)
            else:
                logger.info('Command "%s" was successfully executed.', command_str)
                extracted_information_handler.handle_extracted_information(request, lines_post_processor_instance)
        finally:
            self.handle_on_processing_finished(request)

    def build_command(self, request):
        playbook_path = self.get_playbook_path(request)
        self.ensure_playbook_exists_or_raise(playbook_path)

        command = [settings.WALDUR_ANSIBLE_COMMON.get('PLAYBOOK_EXECUTION_COMMAND', 'ansible-playbook')]

        if settings.WALDUR_ANSIBLE_COMMON.get('PLAYBOOK_ARGUMENTS'):
            command.extend(settings.WALDUR_ANSIBLE_COMMON.get('PLAYBOOK_ARGUMENTS'))

        command.extend(['--extra-vars', self.build_extra_vars(request)])

        command.extend(['--ssh-common-args', '-o UserKnownHostsFile=/dev/null'])

        return command + [playbook_path]

    def ensure_playbook_exists_or_raise(self, playbook_path):
        if not os.path.exists(playbook_path):
            raise exceptions.AnsibleBackendError('Playbook %s does not exist.' % playbook_path)

    def build_extra_vars(self, request):
        extra_vars = self.build_common_extra_vars(request)
        extra_vars.update(self.build_additional_extra_vars(request))
        return json.dumps(extra_vars)

    def build_common_extra_vars(self, request):
        return dict(
            api_url=settings.WALDUR_ANSIBLE_COMMON['API_URL'],
            access_token=RefreshTokenMixin().refresh_token(self.get_user(request)).key,
            private_key_path=settings.WALDUR_ANSIBLE_COMMON['PRIVATE_KEY_PATH'],
            public_key_uuid=settings.WALDUR_ANSIBLE_COMMON['PUBLIC_KEY_UUID'],
        )
