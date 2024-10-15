from io import BytesIO, UnsupportedOperation

import magic
from django.core import files
from django.core.files.storage.base import Storage
from rest_framework.reverse import reverse

from bs4 import BeautifulSoup

from . import models


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

    def _save(self, name, content):
        """Save file with filename `name` and given content to the database."""
        # ZipExtFile advertises seek() but can raise UnsupportedOperation
        try:
            content.seek(0)
        except UnsupportedOperation:
            pass
        content = content.read()
        mime_type = magic.from_buffer(content[:1024], mime=True)
        if mime_type == "image/svg+xml":
            content = remove_scripts(content)
        if isinstance(content, str):
            content = content.encode("utf-8")
        size = len(content)
        models.File.objects.create(
            content=content,
            size=size,
            name=name,
            mime_type=mime_type,
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
