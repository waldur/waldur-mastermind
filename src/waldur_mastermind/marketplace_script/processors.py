from waldur_mastermind.marketplace import processors

from .utils import DockerExecutorMixin

"""
It is expected that offering plugin_options field is dict with following structure:

create:
    image: python:3.7-alpine
    script:
        import os
        print("Creating resource ", os.environ.get('RESOURCE_NAME'))

update:
    image: python:3.7-alpine
    script:
        import os
        print("Updating resource ", os.environ.get('RESOURCE_NAME'))

delete:
    image: python:3.7-alpine
    script:
        import os
        print("Deleting resource ", os.environ.get('RESOURCE_NAME'))

environ:
    USERNAME: admin
    PASSWORD: secret
"""


class CreateProcessor(DockerExecutorMixin, processors.AbstractCreateResourceProcessor):
    hook_type = 'create'


class UpdateProcessor(DockerExecutorMixin, processors.AbstractUpdateResourceProcessor):
    hook_type = 'update'


class DeleteProcessor(DockerExecutorMixin, processors.AbstractDeleteResourceProcessor):
    hook_type = 'delete'
