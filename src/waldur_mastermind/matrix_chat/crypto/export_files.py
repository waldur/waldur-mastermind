"""History export files, encrypted at rest.

An export is a room's history decrypted, so its files are encrypted before they
are stored: each export gets a random 256-bit data key, kept in the export's row
encrypted under FIELD_ENCRYPTION_KEY (an EncryptedTextField, so
``reencrypt_fields`` rotates it with every other encrypted column). Rotating the
field-encryption key therefore never rewrites the files themselves.

A file is a header followed by chunks, each encrypted on its own with
AES-256-GCM, so neither writing nor reading holds a whole plaintext in memory:

    header  = MAGIC | kind (1 byte) | nonce prefix (8 random bytes)
    chunk i = AES-256-GCM(data key, nonce = prefix | i (4 bytes),
                          plaintext = up to CHUNK_BYTES,
                          aad = header | i (8 bytes) | final (1 byte))

The chunk's index and whether it is the last are authenticated, so a chunk
moved, dropped, duplicated or cut off at the end fails to decrypt, and so does
a file of one kind (messages, media) presented as the other. The last chunk may
be empty, so every file ends in an authenticated final chunk.
"""

import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"WMXE1"
KIND_MESSAGES = 1
KIND_MEDIA = 2
# The bot's own working files while it builds an export, never stored.
KIND_SPOOL = 3
KEY_BYTES = 32
PREFIX_BYTES = 8
TAG_BYTES = 16
CHUNK_BYTES = 64 * 1024
HEADER_BYTES = len(MAGIC) + 1 + PREFIX_BYTES
MAX_CHUNKS = 2**32


class ExportFileError(ValueError):
    """The file cannot be decrypted: altered, cut short, or not under this key."""


def new_data_key():
    return AESGCM.generate_key(bit_length=KEY_BYTES * 8)


def _nonce(prefix, index):
    return prefix + struct.pack(">I", index)


def _aad(header, index, final):
    return header + struct.pack(">Q?", index, final)


def _read(source, size):
    """``size`` bytes of ``source``, fewer only at its end: a storage backend may
    return less than asked for at any read, which must not move a chunk's
    boundary."""
    data = source.read(size)
    if len(data) == size or not data:
        return data
    parts = [data]
    remaining = size - len(data)
    while remaining:
        more = source.read(remaining)
        if not more:
            break
        parts.append(more)
        remaining -= len(more)
    return b"".join(parts)


def _chunks(source, size):
    """``(chunk, is_last)`` for every ``size`` bytes of ``source``; one empty
    chunk for an empty source."""
    current = _read(source, size)
    while True:
        following = _read(source, size) if current else b""
        last = not following
        yield current, last
        if last:
            return
        current = following


class EncryptingWriter:
    """A write-only file that encrypts what is written to it into
    ``destination``, a chunk at a time: it holds at most one chunk.

    Not seekable, so ``zipfile`` writes an archive through it as a stream.
    ``close()`` writes the final chunk; a writer closed without it would leave
    a file that never decrypts, as a cut-short one.
    """

    def __init__(self, data_key, kind, destination):
        self._aesgcm = AESGCM(data_key)
        self._header = MAGIC + bytes([kind]) + os.urandom(PREFIX_BYTES)
        self._prefix = self._header[-PREFIX_BYTES:]
        self._destination = destination
        self._buffer = bytearray()
        self._index = 0
        self.closed = False
        destination.write(self._header)

    def write(self, data):
        if self.closed:
            raise ValueError("write to a closed file")
        self._buffer += data
        # A full chunk is written only once more follows it: the last chunk,
        # full or not, is the one marked final.
        while len(self._buffer) > CHUNK_BYTES:
            self._seal(bytes(self._buffer[:CHUNK_BYTES]), last=False)
            del self._buffer[:CHUNK_BYTES]
        return len(data)

    def flush(self):
        pass

    def close(self):
        if self.closed:
            return
        self._seal(bytes(self._buffer), last=True)
        self._buffer.clear()
        self.closed = True

    def _seal(self, plaintext, last):
        if self._index >= MAX_CHUNKS:
            raise ExportFileError("The file is too large to encrypt.")
        self._destination.write(
            self._aesgcm.encrypt(
                _nonce(self._prefix, self._index),
                plaintext,
                _aad(self._header, self._index, last),
            )
        )
        self._index += 1


def encrypt(data_key, kind, source, destination):
    """Encrypt the readable ``source`` into the writable ``destination``."""
    writer = EncryptingWriter(data_key, kind, destination)
    while chunk := _read(source, CHUNK_BYTES):
        writer.write(chunk)
    writer.close()


def _decrypted_chunks(data_key, kind, source):
    if len(data_key) != KEY_BYTES:
        raise ExportFileError("The export's key is not available.")
    header = _read(source, HEADER_BYTES)
    if len(header) != HEADER_BYTES or not header.startswith(MAGIC):
        raise ExportFileError("The file is not an encrypted export.")
    if header[len(MAGIC)] != kind:
        raise ExportFileError("The file is not of the kind asked for.")
    aesgcm = AESGCM(data_key)
    prefix = header[-PREFIX_BYTES:]
    for index, (ciphertext, last) in enumerate(
        _chunks(source, CHUNK_BYTES + TAG_BYTES)
    ):
        if index >= MAX_CHUNKS:
            raise ExportFileError("The file has too many chunks.")
        try:
            yield aesgcm.decrypt(
                _nonce(prefix, index), ciphertext, _aad(header, index, last)
            )
        except InvalidTag:
            raise ExportFileError(
                "The file was altered, cut short, or is not under this key."
            ) from None


def verify(data_key, kind, source):
    """Check every chunk of ``source`` without keeping any plaintext; raises
    ExportFileError. Reading a file only after this passes means a reader never
    receives part of a file that turns out to be altered."""
    for plaintext in _decrypted_chunks(data_key, kind, source):
        del plaintext


def decrypt_chunks(data_key, kind, source):
    """The plaintext of ``source``, one chunk at a time."""
    yield from _decrypted_chunks(data_key, kind, source)


def decrypt_lines(data_key, kind, source):
    """The lines of a file of newline-separated records, decrypted, without
    the newlines; one line in memory at a time, besides one chunk."""
    pending = b""
    for plaintext in _decrypted_chunks(data_key, kind, source):
        pending += plaintext
        *lines, pending = pending.split(b"\n")
        yield from lines
    if pending:
        yield pending
