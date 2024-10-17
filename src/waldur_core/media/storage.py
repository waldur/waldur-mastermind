from io import BytesIO

import magic
from django.core import files
from django.core.files.storage.base import Storage
from rest_framework.reverse import reverse

from bs4 import BeautifulSoup

from . import models, utils


def remove_scripts(svg_string: str):
    soup = BeautifulSoup(svg_string, "xml")

    for script in soup.find_all("script"):
        script.decompose()

    return str(soup)


class DatabaseFile(files.File):
    def close(self):
        pass


class DatabaseStorage(Storage):
    def _open(self, name, mode="rb"):
        try:
            f = models.File.objects.get(name=name)
            content = f.content
            size = f.size
        except models.File.DoesNotExist:
            size = 0
            content = b""
        fh = BytesIO(content)
        fh.name = name
        fh.mode = mode
        fh.size = size
        o = DatabaseFile(fh)
        return o

    def _save(self, name: str, content: files.File):
        content_data = content.read()

        mime_type = magic.from_buffer(content_data[:1024], mime=True)
        if mime_type == "image/svg+xml":
            content_data = remove_scripts(content_data)

        if isinstance(content_data, str):
            content_data = content_data.encode("utf-8")

        content_hash = utils.get_image_hash(content_data)

        models.File.objects.create(
            content=content_data,
            size=len(content_data),
            name=name,
            mime_type=mime_type,
            hash=content_hash,
        )
        return name

    def exists(self, name):
        return models.File.objects.filter(name=name).exists()

    def delete(self, name):
        models.File.objects.filter(name=name).delete()

    def url(self, name):
        try:
            file = models.File.objects.get(name=name)
        except models.File.DoesNotExist:
            return
        else:
            return reverse("media", kwargs={"uuid": file.uuid.hex})

    def size(self, name):
        try:
            return models.File.objects.get(name=name).size
        except models.File.DoesNotExist:
            return 0
