from waldur_mastermind.marketplace import processors

from .utils import DockerExecutorMixin

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


class CreateProcessor(DockerExecutorMixin, processors.AbstractCreateResourceProcessor):
    hook_type = 'create'


class UpdateProcessor(DockerExecutorMixin, processors.AbstractUpdateResourceProcessor):
    hook_type = 'update'


class DeleteProcessor(DockerExecutorMixin, processors.AbstractDeleteResourceProcessor):
    hook_type = 'delete'
