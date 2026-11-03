from django.test import SimpleTestCase

from waldur_firecrest.allowlist import TokenAudience, parse


class AllowlistTest(SimpleTestCase):
    def test_entries(self):
        self.assertEqual(
            parse(
                "https://a.example.org/\n"
                "https://b.example.org|firecrest, "
                "https://c.example.org|firecrest|firecrest-audience"
            ),
            {
                "https://a.example.org": None,
                "https://b.example.org": TokenAudience("firecrest", "firecrest"),
                "https://c.example.org": TokenAudience(
                    "firecrest", "firecrest-audience"
                ),
            },
        )

    def test_empty(self):
        self.assertEqual(parse(""), {})
        self.assertEqual(parse(None), {})
