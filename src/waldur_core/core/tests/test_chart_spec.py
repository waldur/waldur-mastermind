"""Validation, link safety, and label fitting for waldur_core.core.chart_spec."""

from django.test import SimpleTestCase

from waldur_core.core.chart_spec import (
    MAX_ANNOTATIONS,
    MAX_LABEL_LENGTH,
    MAX_POINT_STRING_LENGTH,
    MAX_TITLE_LENGTH,
    ChartSpecSerializer,
    chart,
    fit,
    series,
)


class ChartSpecValidationTest(SimpleTestCase):
    def base_spec(self, **overrides):
        spec = chart(
            "line",
            "Spend",
            {"type": "time"},
            {"type": "value"},
            [series("A", [[1, 2]])],
        )
        return {**spec, **overrides}

    def test_valid_minimal_spec_passes(self):
        serializer = ChartSpecSerializer(data=self.base_spec())
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_boolean_point_values_are_rejected(self):
        spec = self.base_spec(series=[{"name": "A", "points": [[True, False]]}])
        serializer = ChartSpecSerializer(data=spec)
        self.assertFalse(serializer.is_valid())

    def test_overlong_string_point_values_are_rejected(self):
        spec = self.base_spec(
            series=[
                {
                    "name": "A",
                    "points": [["x" * (MAX_POINT_STRING_LENGTH + 1), 1]],
                }
            ]
        )
        serializer = ChartSpecSerializer(data=spec)
        self.assertFalse(serializer.is_valid())

    def test_annotations_count_is_bounded(self):
        annotations = [
            {"type": "line_y", "value": float(i)} for i in range(MAX_ANNOTATIONS + 1)
        ]
        serializer = ChartSpecSerializer(data=self.base_spec(annotations=annotations))
        self.assertFalse(serializer.is_valid())

    def test_area_annotation_requires_value_end(self):
        serializer = ChartSpecSerializer(
            data=self.base_spec(annotations=[{"type": "area_x", "value": 1.0}])
        )
        self.assertFalse(serializer.is_valid())
        serializer_ok = ChartSpecSerializer(
            data=self.base_spec(
                annotations=[{"type": "area_x", "value": 1.0, "value_end": 2.0}]
            )
        )
        self.assertTrue(serializer_ok.is_valid(), serializer_ok.errors)

    def test_axis_min_cannot_exceed_max(self):
        serializer = ChartSpecSerializer(
            data=self.base_spec(y={"type": "value", "min": 10.0, "max": 5.0})
        )
        self.assertFalse(serializer.is_valid())

    def test_secondary_y_axis_requires_y2(self):
        spec = self.base_spec(
            series=[series("A", [[1, 2]], y_axis="secondary")],
        )
        self.assertFalse(ChartSpecSerializer(data=spec).is_valid())
        spec_with_y2 = {**spec, "y2": {"type": "value", "label": "Right"}}
        serializer = ChartSpecSerializer(data=spec_with_y2)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_link_url_rejects_open_redirect_bypasses(self):
        for bad_url in (
            "https://evil.example/path",
            "//evil.example/path",
            "/\\evil.example",
            "/\t/evil.example",
            "/\n/evil.example",
            "/path with space",
        ):
            with self.subTest(url=bad_url):
                serializer = ChartSpecSerializer(
                    data=self.base_spec(link={"label": "Open", "url": bad_url})
                )
                self.assertFalse(serializer.is_valid())

        ok = ChartSpecSerializer(
            data=self.base_spec(
                link={"label": "Open", "url": "/projects/abc/?tab=insights"}
            )
        )
        self.assertTrue(ok.is_valid(), ok.errors)


class ChartSpecFitTest(SimpleTestCase):
    def base_spec(self, **overrides):
        spec = chart(
            "line",
            "Spend",
            {"type": "time"},
            {"type": "value"},
            [series("A", [[1, 2]])],
        )
        return {**spec, **overrides}

    def test_leading_whitespace_title_is_stripped_before_cutting(self):
        fitted = fit(self.base_spec(title=" " * (MAX_TITLE_LENGTH + 5) + "Real Title"))
        self.assertEqual(fitted["title"], "Real Title")
        blank = fit(self.base_spec(title="   "))
        self.assertEqual(blank["title"], "Chart")

    def test_fit_does_not_inject_none_name_on_malformed_series(self):
        fitted = fit(self.base_spec(series=[{"points": [[1, 2]]}]))
        self.assertNotIn("name", fitted["series"][0])

    def test_fit_preserves_explicit_axis_min_and_max_none(self):
        fitted = fit(
            self.base_spec(
                y={"type": "value", "min": None, "max": None, "label": "x" * 150}
            )
        )
        self.assertIn("min", fitted["y"])
        self.assertIsNone(fitted["y"]["min"])
        self.assertEqual(len(fitted["y"]["label"]), MAX_LABEL_LENGTH)

    def test_fit_truncates_long_string_point_values(self):
        fitted = fit(
            self.base_spec(
                series=[
                    series("A", [["x" * (MAX_POINT_STRING_LENGTH + 20), 1]]),
                ]
            )
        )
        value = fitted["series"][0]["points"][0][0]
        assert isinstance(value, str)
        self.assertEqual(len(value), MAX_POINT_STRING_LENGTH)
        serializer = ChartSpecSerializer(data=fitted)
        self.assertTrue(serializer.is_valid(), serializer.errors)
