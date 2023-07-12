import base64
import json
import logging

from waldur_mastermind.marketplace import processors

from .utils import ContainerExecutorMixin

"""
It is expected that offering plugin_options field is dict with following structure:

language: python

environ:
    USERNAME: admin
    PASSWORD: secret

create:
    import os
    print("Creating resource ", os.environ.get('RESOURCE_NAME'))

update:
    import os
    print("Updating resource ", os.environ.get('RESOURCE_NAME'))

delete:
    import os
    print("Deleting resource ", os.environ.get('RESOURCE_NAME'))

pull:
    import os
    print("Pulling resource ", os.environ.get('RESOURCE_NAME'))
"""

logger = logging.getLogger(__name__)


class CreateProcessor(
    ContainerExecutorMixin, processors.AbstractCreateResourceProcessor
):
    hook_type = 'create'

    def send_request(self, user):
        output = super().send_request(user)
        if output:
            last_line = output.splitlines()[-1].split()
            if len(last_line) == 1:
                # return the last line of the output as a backend_id of a created resource
                return last_line[0]
            elif len(last_line) == 2:
                # expecting space separated backend_id and base64-encoded metadata in json format
                result = {'response_type': 'metadata'}
                if str(last_line[0]) == 'null':
                    raise ValueError('Backend id returned as null, will not proceed.')
                result['backend_id'] = str(last_line[0])
                decoded_metadata = base64.b64decode(last_line[1])
                try:
                    result['backend_metadata'] = json.loads(decoded_metadata)
                except ValueError:
                    logger.error(
                        f'Failed to encode as json metadata: {decoded_metadata}'
                    )
                return result
            else:
                logger.error('Unexpected structure of output', last_line)
                raise


class UpdateProcessor(
    ContainerExecutorMixin, processors.AbstractUpdateResourceProcessor
):
    hook_type = 'update'

    def send_request(self, user):
        super().send_request(user)
        return True


class DeleteProcessor(
    ContainerExecutorMixin, processors.AbstractDeleteResourceProcessor
):
    hook_type = 'delete'

    def send_request(self, user, resource):
        super().send_request(user, resource)
        return True
