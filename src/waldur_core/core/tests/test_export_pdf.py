import base64
import io
import struct
import subprocess  # noqa: S404
import tempfile
import zlib
from pathlib import Path
from unittest import mock

import pypdf
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.core import pdf
from waldur_core.core import serializers as core_serializers
from waldur_core.core import views as core_views
from waldur_core.structure.tests import fixtures


def make_png(width, height, compressed=True):
    def chunk(kind, body):
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = b"".join(b"\x00" + b"\xc8\x32\x32" * width for _ in range(height))
    return (
        core_serializers.PNG_SIGNATURE
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(pixels) if compressed else pixels)
        + chunk(b"IEND", b"")
    )


def png_header(width, height):
    """Only the signature and IHDR of a PNG: enough for the size checks."""
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        core_serializers.PNG_SIGNATURE
        + struct.pack(">I", len(header))
        + b"IHDR"
        + header
        + b"\x00" * 4
    )


class ExportPdfTest(APITestCase):
    url = "/api/export-pdf/"

    def setUp(self):
        self.client.force_authenticate(fixtures.UserFixture().user)

    def post(self, payload):
        return self.client.post(self.url, payload, format="json")

    def pdf_text(self, response):
        reader = pypdf.PdfReader(io.BytesIO(response.content))
        return "\n".join(page.extract_text() for page in reader.pages)

    def pdf_fonts(self, response):
        reader = pypdf.PdfReader(io.BytesIO(response.content))
        return {
            str(font["/BaseFont"])
            for page in reader.pages
            for font in page["/Resources"]["/Font"].values()
        }

    def test_table_holds_title_headers_and_cells(self):
        response = self.post(
            {
                "title": "Resources",
                "columns": ["Name", "State", "Cost"],
                "rows": [
                    ["Alpha VM", "OK", "12.50"],
                    ["Beta volume", "Erred", ""],
                ],
            }
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(
            response["Content-Disposition"], 'attachment; filename="Resources.pdf"'
        )
        text = self.pdf_text(response)
        for value in (
            "Resources",
            "Name",
            "State",
            "Cost",
            "Alpha VM",
            "OK",
            "12.50",
            "Beta volume",
            "Erred",
        ):
            self.assertIn(value, text)

    def test_header_repeats_on_every_page(self):
        response = self.post(
            {
                "title": "Long",
                "columns": ["Header A", "Header B"],
                "rows": [[f"row {i}", "x"] for i in range(300)],
            }
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        reader = pypdf.PdfReader(io.BytesIO(response.content))
        self.assertGreater(len(reader.pages), 1)
        for page in reader.pages:
            self.assertIn("Header A", page.extract_text())

    def test_markup_in_cells_prints_literally(self):
        markup = "#set page(width: 1pt) $x^2$ *bold* _em_ [a] \\ @ref <label>"
        response = self.post(
            {"title": "#title *x*", "columns": ["$col$"], "rows": [[markup]]}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        text = self.pdf_text(response)
        self.assertIn("#title *x*", text)
        self.assertIn("$col$", text)
        self.assertIn(markup, text)

    def test_long_words_wrap_without_losing_characters(self):
        token = "6f1c2e3d4b5a69788796a5b4c3d2e1f0"
        word = "Достопримечательности"  # 21 letters, 42 bytes
        response = self.post(
            {
                "title": "Wide",
                "columns": [f"Column {i}" for i in range(20)],
                "rows": [[token, word] + ["lorem ipsum"] * 18],
            }
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        text = self.pdf_text(response)
        # Zero-width spaces mark where a long word may break.
        self.assertIn(token, text.replace("\u200b", "").replace("\n", ""))
        self.assertIn(word, text.replace("\u200b", "").replace("\n", ""))

    def test_latin_cyrillic_and_greek_use_the_bundled_font(self):
        values = ["Grüße", "Привет, мир", "Γειά σου κόσμε"]
        response = self.post(
            {"title": "Fonts", "columns": ["Text"], "rows": [[v] for v in values]}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        text = self.pdf_text(response)
        for value in values:
            self.assertIn(value, text)
        self.assertTrue(self.pdf_fonts(response))
        for font in self.pdf_fonts(response):
            self.assertIn("SourceSans3", font)

    def test_chart_image_is_embedded(self):
        image = base64.b64encode(make_png(40, 20)).decode()

        response = self.post({"title": "Usage", "image": image})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        reader = pypdf.PdfReader(io.BytesIO(response.content))
        self.assertEqual(len(reader.pages[0].images), 1)
        self.assertIn("Usage", reader.pages[0].extract_text())

    def test_tall_chart_stays_on_the_page_with_its_title(self):
        image = base64.b64encode(make_png(600, 900)).decode()

        response = self.post({"title": "Tall", "image": image})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        reader = pypdf.PdfReader(io.BytesIO(response.content))
        self.assertEqual(len(reader.pages), 1)
        self.assertEqual(len(reader.pages[0].images), 1)

    def test_file_name_drops_path_separators(self):
        response = self.post({"title": "../a/b\\c", "columns": ["A"], "rows": []})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response["Content-Disposition"], 'attachment; filename=".._a_b_c.pdf"'
        )

    def test_file_name_drops_reserved_characters_and_is_capped(self):
        response = self.post(
            {
                "title": ('Usage: Q1 <draft>?*|"' + "x" * 255)[:255],
                "columns": ["A"],
                "rows": [],
            }
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        name = response["Content-Disposition"].split('filename="')[1].rstrip('"')
        self.assertTrue(name.startswith("Usage_ Q1 _draft____"))
        self.assertLessEqual(len(name), 154)

    def test_untitled_export_is_named_export(self):
        response = self.post({"title": "", "columns": ["A"], "rows": [["1"]]})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response["Content-Disposition"], 'attachment; filename="export.pdf"'
        )

    def test_anonymous_user_gets_401(self):
        self.client.force_authenticate(None)

        response = self.post({"title": "T", "columns": ["A"], "rows": []})

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class ExportPdfValidationTest(APITestCase):
    url = "/api/export-pdf/"

    def setUp(self):
        self.client.force_authenticate(fixtures.UserFixture().user)

    def assert_rejected(self, payload, field, message):
        with mock.patch.object(pdf, "render_pdf_isolated") as render:
            response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(message, str(response.data[field]))
        render.assert_not_called()

    def test_too_many_rows(self):
        self.assert_rejected(
            {"title": "T", "columns": ["A"], "rows": [["x"]] * 10_001},
            "rows",
            "10000 rows",
        )

    def test_too_many_columns(self):
        self.assert_rejected(
            {"title": "T", "columns": ["c"] * 101, "rows": []},
            "columns",
            "100 columns",
        )

    def test_rows_that_are_not_lists_still_count(self):
        self.assert_rejected(
            {"title": "T", "columns": ["A"], "rows": [0] * 10_001},
            "rows",
            "10000 rows",
        )

    def test_header_alone_is_held_to_the_character_budget(self):
        self.assert_rejected(
            {"title": "T", "columns": ["x" * 500_001, "y" * 500_000]},
            "rows",
            "1000000 characters",
        )

    def test_too_many_cells(self):
        self.assert_rejected(
            {"title": "T", "columns": ["c"] * 10, "rows": [["x"] * 10] * 5_001},
            "rows",
            "50000 cells",
        )

    def test_too_many_characters(self):
        self.assert_rejected(
            {"title": "T", "columns": ["c"] * 2, "rows": [["x" * 50_001] * 2] * 10},
            "rows",
            "1000000 characters",
        )

    def test_character_budget_is_checked_before_each_cell(self):
        # The null cell would fail validation, but the budget is checked first,
        # so an oversized table is refused without validating every cell.
        self.assert_rejected(
            {"title": "T", "columns": ["c", "d"], "rows": [["x" * 1_000_001, None]]},
            "rows",
            "1000000 characters",
        )

    def test_row_with_wrong_number_of_cells(self):
        self.assert_rejected(
            {"title": "T", "columns": ["A", "B"], "rows": [["1", "2"], ["3"]]},
            "rows",
            "Row 2 has 1 cells, not 2.",
        )

    def test_image_over_size_limit(self):
        image = make_png(1, 1) + b"\x00" * (5 * 1024 * 1024)
        self.assert_rejected(
            {"title": "T", "image": base64.b64encode(image).decode()},
            "image",
            "5 MB",
        )

    def test_image_that_is_not_png(self):
        self.assert_rejected(
            {"title": "T", "image": base64.b64encode(b"GIF89a....").decode()},
            "image",
            "must be a PNG",
        )

    def test_image_that_is_not_base64(self):
        self.assert_rejected({"title": "T", "image": "not base64!"}, "image", "base64")

    def test_image_over_pixel_limit(self):
        self.assert_rejected(
            {"title": "T", "image": base64.b64encode(png_header(5001, 5000)).decode()},
            "image",
            "25000000 pixels",
        )

    def test_image_without_size(self):
        self.assert_rejected(
            {"title": "T", "image": base64.b64encode(png_header(0, 10)).decode()},
            "image",
            "must be a PNG",
        )

    def test_image_cut_off_inside_its_header(self):
        self.assert_rejected(
            {"title": "T", "image": base64.b64encode(png_header(10, 10)[:20]).decode()},
            "image",
            "must be a PNG",
        )

    def test_table_and_image_together(self):
        image = base64.b64encode(make_png(1, 1)).decode()
        self.assert_rejected(
            {"title": "T", "columns": ["A"], "rows": [], "image": image},
            "non_field_errors",
            "either columns and rows, or an image",
        )

    def test_neither_table_nor_image(self):
        self.assert_rejected(
            {"title": "T"}, "non_field_errors", "either columns and rows, or an image"
        )

    def test_rows_without_columns(self):
        self.assert_rejected({"title": "T", "rows": [["x"]]}, "columns", "required")

    def test_render_timeout_is_a_400(self):
        with mock.patch.object(
            pdf.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("worker", pdf.ISOLATED_TIMEOUT),
        ):
            response = self.client.post(
                self.url, {"title": "T", "columns": ["A"], "rows": []}, format="json"
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("too long to render", str(response.data))

    def test_corrupt_png_is_a_400(self):
        # A valid header, but the image data does not decode.
        image = png_header(10, 10) + b"\x00\x00\x00\x04IDATjunk\x00\x00\x00\x00"

        response = self.client.post(
            self.url,
            {"title": "T", "image": base64.b64encode(image).decode()},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("could not be read", str(response.data))

    def test_table_that_fails_to_render_is_a_server_error(self):
        # Validated text always renders, so a failure is not the client's.
        self.client.raise_request_exception = False
        with mock.patch.object(
            pdf, "render_pdf_isolated", side_effect=pdf.PdfRenderError("boom")
        ):
            response = self.client.post(
                self.url, {"title": "T", "columns": ["A"], "rows": []}, format="json"
            )

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    def test_second_export_while_one_renders_is_refused(self):
        user = fixtures.UserFixture().user
        self.client.force_authenticate(user)
        core_views.cache.add(f"export-pdf:{user.pk}", True)
        try:
            response = self.client.post(
                self.url, {"title": "T", "columns": ["A"], "rows": []}, format="json"
            )
        finally:
            core_views.cache.delete(f"export-pdf:{user.pk}")

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn("still rendering", str(response.data))

    def test_lock_is_released_after_an_export(self):
        payload = {"title": "T", "columns": ["A"], "rows": [["1"]]}
        with mock.patch.object(pdf, "render_pdf_isolated", return_value=b"%PDF"):
            first = self.client.post(self.url, payload, format="json")
            second = self.client.post(self.url, payload, format="json")

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)


class RenderPdfIsolatedTest(APITestCase):
    def test_worker_failure_raises_with_its_stderr(self):
        with self.assertRaisesRegex(pdf.PdfRenderError, "exit code"):
            pdf.render_pdf_isolated(pdf.EXPORT_TEMPLATE, {"no": "title"})

    def test_worker_runs_without_the_preloaded_allocator(self):
        with (
            mock.patch.dict(pdf.os.environ, {"LD_PRELOAD": "libjemalloc.so.2"}),
            mock.patch.object(pdf.subprocess, "run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 0, b"%PDF", b"")
            pdf.render_pdf_isolated(pdf.EXPORT_TEMPLATE, {"title": ""})

        env = run.call_args.kwargs["env"]
        self.assertNotIn("LD_PRELOAD", env)
        self.assertEqual(env["RAYON_NUM_THREADS"], "2")
        self.assertEqual(env["MALLOC_ARENA_MAX"], "2")

    def test_template_cannot_read_outside_its_directory(self):
        # Data is never evaluated, but even a template that tried could not
        # reach a file outside the temporary root it is rendered in.
        with tempfile.TemporaryDirectory() as directory:
            template = Path(directory) / "probe.typ"
            template.write_text('#read("../../../../../../../../etc/hosts")')
            with self.assertRaisesRegex(pdf.PdfRenderError, "escape the project root"):
                pdf.render_pdf_isolated(template, {})
