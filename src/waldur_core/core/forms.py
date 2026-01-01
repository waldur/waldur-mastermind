import json

from django import forms


class MultilingualImageField(forms.CharField):
    """
    Form field for storing language-specific image paths as a dictionary.

    Stores data as JSON: {"de": "path/to/german.png", "et": "path/to/estonian.png"}
    Used for constance settings that need language-specific variations.
    """

    def __init__(self, *args, **kwargs):
        kwargs["widget"] = forms.Textarea
        super().__init__(*args, **kwargs)

    def to_python(self, value):
        if not value:
            return {}
        if isinstance(value, dict):
            return value
        try:
            return json.loads(value)
        except ValueError as e:
            raise forms.ValidationError(f"Invalid JSON format: {str(e)}")

    def prepare_value(self, value):
        if value is None:
            return ""
        if isinstance(value, dict):
            try:
                return json.dumps(value, indent=2)
            except (TypeError, ValueError) as e:
                raise forms.ValidationError(f"Could not serialize dictionary: {str(e)}")
        return value
