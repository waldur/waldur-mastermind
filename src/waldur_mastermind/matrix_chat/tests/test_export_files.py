import base64
import io
import json
import os
import zipfile
from io import StringIO
from unittest import mock

from cryptography.fernet import Fernet
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import connection
from django.db.models.fields.files import FieldFile
from django.test import SimpleTestCase, override_settings
from rest_framework import status, test

from waldur_core.media.storage import DatabaseStorage
from waldur_mastermind.matrix_chat import bot_export, bot_state, models, tasks, views
from waldur_mastermind.matrix_chat.crypto import export_files
from waldur_mastermind.matrix_chat.tests import fixtures

CHUNK = export_files.CHUNK_BYTES
SEALED = CHUNK + export_files.TAG_BYTES
HEADER = export_files.HEADER_BYTES


def seal(plaintext, kind=export_files.KIND_MESSAGES, key=None):
    key = key or export_files.new_data_key()
    sealed = io.BytesIO()
    export_files.encrypt(key, kind, io.BytesIO(plaintext), sealed)
    return key, sealed.getvalue()


def open_sealed(key, sealed, kind=export_files.KIND_MESSAGES):
    return b"".join(export_files.decrypt_chunks(key, kind, io.BytesIO(sealed)))


class EncryptedFileTest(SimpleTestCase):
    def test_round_trip(self):
        for size in (0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK + 17):
            with self.subTest(size=size):
                plaintext = os.urandom(size)
                key, sealed = seal(plaintext)
                if size >= 64:
                    self.assertNotIn(plaintext[:64], sealed)
                self.assertEqual(open_sealed(key, sealed), plaintext)

    def assertRefused(self, key, sealed, kind=export_files.KIND_MESSAGES):
        with self.assertRaises(export_files.ExportFileError):
            export_files.verify(key, kind, io.BytesIO(sealed))
        with self.assertRaises(export_files.ExportFileError):
            open_sealed(key, sealed, kind)

    def test_a_flipped_byte_is_refused(self):
        key, sealed = seal(os.urandom(2 * CHUNK))
        for position in (0, HEADER - 1, HEADER, HEADER + SEALED + 5, len(sealed) - 1):
            with self.subTest(position=position):
                altered = bytearray(sealed)
                altered[position] ^= 1
                self.assertRefused(key, bytes(altered))

    def test_a_file_cut_short_is_refused(self):
        key, sealed = seal(os.urandom(2 * CHUNK + 10))
        # Mid-chunk, at a chunk boundary (the dropped chunks were the last
        # ones, so the one left is not marked final), and down to the header.
        for length in (len(sealed) - 5, HEADER + 2 * SEALED, HEADER + SEALED, HEADER):
            with self.subTest(length=length):
                self.assertRefused(key, sealed[:length])

    def test_moved_dropped_or_added_chunks_are_refused(self):
        key, sealed = seal(os.urandom(3 * CHUNK))
        header, body = sealed[:HEADER], sealed[HEADER:]
        chunks = [body[i : i + SEALED] for i in range(0, len(body), SEALED)]
        cases = {
            "swapped": [chunks[1], chunks[0], *chunks[2:]],
            "dropped": [chunks[0], *chunks[2:]],
            "duplicated": [chunks[0], *chunks],
            "appended": [*chunks, b"extra"],
        }
        for name, reordered in cases.items():
            with self.subTest(name):
                self.assertRefused(key, header + b"".join(reordered))

    def test_a_chunk_of_another_file_under_the_same_key_is_refused(self):
        key, first = seal(os.urandom(CHUNK))
        _, second = seal(os.urandom(CHUNK), key=key)
        self.assertRefused(key, first[:HEADER] + second[HEADER:])

    def test_another_key_is_refused(self):
        _, sealed = seal(b"history")
        self.assertRefused(export_files.new_data_key(), sealed)
        self.assertRefused(b"short", sealed)

    def test_messages_cannot_pass_for_media(self):
        key, sealed = seal(b"history", kind=export_files.KIND_MESSAGES)
        self.assertRefused(key, sealed, kind=export_files.KIND_MEDIA)
        # Not even with the kind byte changed: it is authenticated.
        altered = bytearray(sealed)
        altered[len(export_files.MAGIC)] = export_files.KIND_MEDIA
        self.assertRefused(key, bytes(altered), kind=export_files.KIND_MEDIA)

    def test_short_reads_do_not_move_chunk_boundaries(self):
        class Trickle(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 1000) if size > 0 else size)

        plaintext = os.urandom(3 * CHUNK + 5)
        key = export_files.new_data_key()
        sealed = io.BytesIO()
        export_files.encrypt(key, export_files.KIND_MEDIA, Trickle(plaintext), sealed)
        self.assertEqual(
            b"".join(
                export_files.decrypt_chunks(
                    key, export_files.KIND_MEDIA, Trickle(sealed.getvalue())
                )
            ),
            plaintext,
        )

    def test_memory_is_bounded_by_the_chunk(self):
        class Reader(io.BytesIO):
            largest = 0

            def read(self, size=-1):
                assert size > 0, "the whole file was read at once"
                Reader.largest = max(Reader.largest, size)
                return super().read(size)

        plaintext = os.urandom(5 * CHUNK)
        key = export_files.new_data_key()
        sealed = io.BytesIO()
        export_files.encrypt(key, export_files.KIND_MEDIA, Reader(plaintext), sealed)
        self.assertLessEqual(Reader.largest, CHUNK)
        Reader.largest = 0
        pieces = list(
            export_files.decrypt_chunks(
                key, export_files.KIND_MEDIA, Reader(sealed.getvalue())
            )
        )
        self.assertLessEqual(Reader.largest, SEALED)
        self.assertTrue(all(len(piece) <= CHUNK for piece in pieces))
        self.assertEqual(b"".join(pieces), plaintext)


def _claimed_export(room):
    models.MatrixHistoryExport.objects.create(
        room=room, export_type=models.ExportTypes.MANUAL
    )
    return bot_state.claim_next_export()


MESSAGES = [{"event_id": "$1", "body": "a decrypted secret"}]


def _media_zip():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("_1_photo.jpg", b"decrypted photo")
    archive.seek(0)
    return archive


def prepared(messages, media_zip=None):
    """Export files as the bot hands them over for storing."""
    data_key = export_files.new_data_key()
    sealed_messages = io.BytesIO()
    writer = export_files.EncryptingWriter(
        data_key, export_files.KIND_MESSAGES, sealed_messages
    )
    header = {
        "room_id": "!test_room:matrix.example.com",
        "room_name": "Room",
        "exported_at": "2026-10-10T00:00:00+00:00",
        "message_count": len(messages),
        "media_count": 1 if media_zip else 0,
    }
    bot_export.write_export_document(writer, header, iter(messages))
    writer.close()
    sealed_messages.seek(0)
    sealed_media = None
    if media_zip is not None:
        sealed_media = io.BytesIO()
        export_files.encrypt(data_key, export_files.KIND_MEDIA, media_zip, sealed_media)
        sealed_media.seek(0)
    return bot_export.ExportFiles(
        data_key, sealed_messages, sealed_media, len(messages), header["media_count"]
    )


class EncryptedExportTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.export = _claimed_export(self.fixture.matrix_room)
        tasks.save_history_export(self.export, prepared(MESSAGES, _media_zip()))
        self.export.refresh_from_db()
        self.client.force_authenticate(self.fixture.staff)

    def _get(self, kind):
        return self.client.get(
            f"/api/matrix/exports/{self.export.uuid}/download/{kind}/"
        )

    def _stored(self, file_field):
        with file_field.open("rb") as stored:
            return stored.read()

    def test_files_are_stored_encrypted(self):
        for file_field in (self.export.export_file, self.export.media_file):
            stored = self._stored(file_field)
            self.assertTrue(stored.startswith(export_files.MAGIC))
            self.assertNotIn(b"a decrypted secret", stored)
            self.assertNotIn(b"decrypted photo", stored)

    def test_the_data_key_is_stored_under_the_field_encryption_key(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT data_key FROM matrix_chat_matrixhistoryexport WHERE id = %s",
                [self.export.pk],
            )
            (raw,) = cursor.fetchone()
        self.assertTrue(raw.startswith("gAAAA"))
        self.assertNotIn(self.export.data_key, raw)
        self.assertEqual(len(base64.b64decode(self.export.data_key)), 32)

    def test_downloads_decrypted(self):
        response = self._get("export")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn(".json", response["Content-Disposition"])
        data = json.loads(b"".join(response.streaming_content))
        self.assertEqual(data["messages"], MESSAGES)

        response = self._get("media")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        with zipfile.ZipFile(io.BytesIO(b"".join(response.streaming_content))) as zf:
            self.assertEqual(zf.read("_1_photo.jpg"), b"decrypted photo")

    def _replace_stored(self, content):
        name = self.export.export_file.name
        self.export.export_file.storage.delete(name)
        self.export.export_file.storage.save(name, ContentFile(content))

    def test_an_altered_file_is_refused_with_nothing_of_it(self):
        stored = bytearray(self._stored(self.export.export_file))
        stored[-1] ^= 1
        self._replace_stored(bytes(stored))
        response = self._get("export")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertNotIn(b"a decrypted secret", response.content)
        self.assertFalse(getattr(response, "streaming", False))

    def test_a_file_cut_short_is_refused(self):
        stored = self._stored(self.export.export_file)
        self._replace_stored(stored[: len(stored) - 3])
        self.assertEqual(self._get("export").status_code, status.HTTP_409_CONFLICT)

    def test_a_lost_field_encryption_key_makes_it_unreadable(self):
        with override_settings(
            FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode(),
            FIELD_ENCRYPTION_KEY_FALLBACKS=[],
            SECRET_KEY="another-secret-key",
        ):
            response = self._get("export")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_the_file_is_read_once_opened(self):
        real_open = FieldFile.open
        opened = []

        def counting_open(field_file, mode="rb"):
            opened.append(field_file.name)
            return real_open(field_file, mode)

        with mock.patch.object(FieldFile, "open", counting_open):
            response = self._get("export")
            b"".join(response.streaming_content)
        self.assertEqual(len(opened), 1)

    def test_a_file_gone_from_storage_is_not_found(self):
        # The database storage, which opens a missing file as an empty one.
        self.assertIsInstance(self.export.export_file.storage, DatabaseStorage)
        self.export.export_file.storage.delete(self.export.export_file.name)
        self.assertEqual(self._get("export").status_code, status.HTTP_404_NOT_FOUND)

    def test_a_storage_that_fails_to_open_is_not_found_either(self):
        with mock.patch.object(FieldFile, "open", side_effect=FileNotFoundError):
            self.assertEqual(self._get("export").status_code, status.HTTP_404_NOT_FOUND)

    def test_downloads_are_not_cached(self):
        for kind in ("export", "media"):
            with self.subTest(kind):
                response = self._get(kind)
                self.assertEqual(response["Cache-Control"], "no-store")
                b"".join(response.streaming_content)

    def test_downloads_are_throttled_per_user(self):
        self.assertEqual(
            views.MatrixHistoryExportDownloadView.throttle_scope,
            "matrix_export_download",
        )

    def test_a_file_changed_while_it_is_sent_aborts_the_download(self):
        key, sealed = seal(os.urandom(3 * CHUNK))
        altered = bytearray(sealed)
        altered[-1] ^= 1
        stored = io.BytesIO(bytes(altered))
        stream = views._DecryptedStream(
            stored, key, export_files.KIND_MESSAGES, self.export, "export"
        )
        received = []
        with self.assertRaises(export_files.ExportFileError):
            for chunk in stream:
                received.append(chunk)
        # The chunks before the altered one went out; the altered one did not.
        self.assertEqual(len(received), 2)
        stream.close()
        self.assertTrue(stored.closed)

    def test_the_stored_file_is_closed_even_if_nothing_was_sent(self):
        key, sealed = seal(b"history")
        stored = io.BytesIO(sealed)
        views._DecryptedStream(
            stored, key, export_files.KIND_MESSAGES, self.export, "export"
        ).close()
        self.assertTrue(stored.closed)
        response = self._get("export")
        response.close()

    def test_a_legacy_export_gone_from_storage_is_not_found(self):
        legacy = self.fixture.history_export
        legacy.export_file.save("history.json", ContentFile(b'{"messages": []}'))
        legacy.export_file.storage.delete(legacy.export_file.name)
        response = self.client.get(
            f"/api/matrix/exports/{legacy.uuid}/download/export/"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_an_export_written_before_encryption_still_downloads(self):
        legacy = self.fixture.history_export
        legacy.export_file.save("history.json", ContentFile(b'{"messages": []}'))
        self.assertEqual(legacy.data_key, "")
        response = self.client.get(
            f"/api/matrix/exports/{legacy.uuid}/download/export/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(b"".join(response.streaming_content), b'{"messages": []}')


OLD_KEY = Fernet.generate_key().decode()
NEW_KEY = Fernet.generate_key().decode()


class DataKeyRotationTest(test.APITestCase):
    def test_reencrypt_fields_rotates_the_data_key_and_files_stay_readable(self):
        fixture = fixtures.MatrixChatFixture()
        with override_settings(
            FIELD_ENCRYPTION_KEY=OLD_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            export = _claimed_export(fixture.matrix_room)
            tasks.save_history_export(export, prepared(MESSAGES))
        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[OLD_KEY]
        ):
            call_command("reencrypt_fields", stdout=StringIO())
        self.client.force_authenticate(fixture.staff)
        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            response = self.client.get(
                f"/api/matrix/exports/{export.uuid}/download/export/"
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = json.loads(b"".join(response.streaming_content))
        self.assertEqual(data["messages"], MESSAGES)
