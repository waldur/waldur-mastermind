import os
import docker

from waldur_ansible.common.tests.integration.image import DockerImage


class Ubuntu1604Image(DockerImage):
    PRIVATE_KEY_PATH = os.path.dirname(os.path.abspath(__file__)) + "/ubuntu1604_image/waldur_integration_test_ssh_key"
    IMAGE_NAME = 'integration-test-image:ubuntu1604'
    IMAGE_DIR_NAME = 'ubuntu1604_image'

    def __init__(self):
        super(Ubuntu1604Image, self).__init__(
            Ubuntu1604Image.IMAGE_NAME,
            Ubuntu1604Image.IMAGE_DIR_NAME,
            'https://cloud-images.ubuntu.com/xenial/current/xenial-server-cloudimg-amd64-root.tar.gz',
            'xenial-server-cloudimg',
            'tar.gz', )

    def post_process_image(self):
        docker.from_env().containers.run(
            Ubuntu1604Image.IMAGE_NAME,
            auto_remove=True,
            privileged=True,
            volumes=['/:/host'],
            command='/bin/bash -c setup')

    def get_private_key_path(self):
        return '%s/%s/%s' % (os.path.dirname(os.path.abspath(__file__)), Ubuntu1604Image.IMAGE_DIR_NAME, 'waldur_integration_test_ssh_key')
