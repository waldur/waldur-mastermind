import os

import docker
import requests


class DockerImage(object):
    def __init__(self, image_name, image_dir_name, base_image_download_url, base_image_file_name, base_image_file_extension):
        self.image_name = image_name
        self.image_dir_name = image_dir_name
        self.base_image_download_url = base_image_download_url
        self.base_image_file_name = base_image_file_name
        self.base_image_file_extension = base_image_file_extension

    def build_image(self, force_rebuild=False):
        if force_rebuild or not self.image_exists():
            base_image_file_path = self.download_base_image()
            self.import_base_image(base_image_file_path)
            self.build_image_from_dockerfile()
            self.post_process_image()

    def import_base_image(self, base_image_file_name):
        docker.APIClient().import_image(src=base_image_file_name, repository=self.base_image_file_name)

    def download_base_image(self):
        base_image_file_path = '%s/%s/%s.%s' % (os.path.dirname(os.path.abspath(__file__)), self.image_dir_name, self.base_image_file_name, self.base_image_file_extension)
        if not os.path.isfile(base_image_file_path):
            downloaded_image_fie = requests.get(self.base_image_download_url)
            with open(base_image_file_path, 'wb') as f:
                f.write(downloaded_image_fie.content)
        return base_image_file_path

    def build_image_from_dockerfile(self):
        _, result_stream = docker.from_env().images.build(rm=True, path='%s/%s' % (os.path.dirname(os.path.abspath(__file__)), self.image_dir_name), tag=self.image_name)
        for chunk in result_stream:
            print(chunk.get('stream', chunk))

    def image_exists(self):
        return docker.from_env().images.list(name=self.image_name)

    def post_process_image(self):
        """
        If you need to perform any additional steps, override this method
        """
        pass
