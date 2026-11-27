from io import BytesIO

from bs4 import BeautifulSoup
from django.core import files
from django.core.files.storage.base import Storage
from rest_framework.reverse import reverse

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

        import magic

        mime_type = magic.from_buffer(content_data[:1024], mime=True)
        if mime_type in ("image/svg", "image/svg+xml"):
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

    # The file's content is a column of its row: reading the row whole to
    # answer for its link or size would load the file each time.
    def url(self, name):
        uuid = (
            models.File.objects.filter(name=name).values_list("uuid", flat=True).first()
        )
        if uuid is None:
            return
        return reverse("media", kwargs={"uuid": uuid.hex})

    def size(self, name):
        size = (
            models.File.objects.filter(name=name).values_list("size", flat=True).first()
        )
        return 0 if size is None else size
