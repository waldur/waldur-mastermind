# Constance Settings Documentation

Waldur uses [django-constance](https://django-constance.readthedocs.io/en/latest/) to manage dynamic configuration via the database. This allows administrators to change settings through the UI without restarting the application.

## Configuration Structure

Settings are defined in `src/waldur_core/server/constance_settings.py` within the `CONSTANCE_CONFIG` dictionary.

Waldur extends the standard Constance configuration using a **3-element tuple** format for basic metadata, and a separate `CONSTANCE_CONFIG_CHOICES` dictionary for enum options:

### 1. Basic Configuration (`CONSTANCE_CONFIG`)

```python
"SETTING_KEY": (
    default_value,
    "Description of the setting.",
    "field_type"
)
```

1.  **Default Value**: The initial value (and type if field type is not specified).
2.  **Description**: A human-readable description (used for tooltips and help text).
3.  **Field Type**: A string identifying the UI component and backend validator to use.

### 2. Enum Choices (`CONSTANCE_CONFIG_CHOICES`)

To provide options for `choice_field` or `multiple_choice_field`, add an entry to `CONSTANCE_CONFIG_CHOICES`:

```python
CONSTANCE_CONFIG_CHOICES = {
    "SETTING_KEY": [
        ("value1", "Label 1"),
        ("value2", "Label 2"),
    ],
}
```

## Custom Field Types

Waldur implements several custom field types to enhance the configuration experience:

| Type | Description | Component | Options Required? |
|------|-------------|-----------|-------------------|
| `choice_field` | Single selection from a predefined list. | Select | Yes |
| `multiple_choice_field` | Multiple selection from a predefined list. | Multi-Select/Badges | Yes |
| `list_field` | Comma-separated list of strings. | Text/List | No |
| `dict_field` | JSON object (rendered with syntax highlighting). | Monaco Editor | No |
| `json_list_field` | List of JSON objects. | Monaco Editor | No |
| `html_field` | HTML content (with syntax highlighting). | Monaco Editor | No |
| `color_field` | Hex color code. | Color Picker | No |
| `image_field` | Image upload. | File Upload | No |
| `secret_field` | Sensitive data (masked in UI). | Password Input | No |
| `non_empty_field` | String that may be changed but not blanked out. | Text Input | No |

## Practical Examples

### 1. Simple String Setting
By default, if only a default value and description are provided, it is treated as a string or boolean based on the default value's type.

```python
"SITE_NAME": ("Waldur", "Human-friendly name of the Waldur deployment."),
```

### 2. Single Choice Enum
Use `choice_field` and define choices in `CONSTANCE_CONFIG_CHOICES`.

```python
SIDEBAR_STYLE_CHOICES = [
    ("primary", "Primary"),
    ("dark", "Dark"),
    ("light", "Light"),
]

# In CONSTANCE_CONFIG
"SIDEBAR_STYLE": (
    "dark",
    "Style of sidebar.",
    "choice_field",
),

# In CONSTANCE_CONFIG_CHOICES
CONSTANCE_CONFIG_CHOICES = {
    "SIDEBAR_STYLE": SIDEBAR_STYLE_CHOICES,
}
```

### 3. Multiple Choice Enum
Use `multiple_choice_field` for settings that accept a list of predefined options.

```python
USER_ATTRIBUTE_CHOICES = [
    ("username", "Username"),
    ("full_name", "Full name"),
    ("email", "Email"),
]

# In CONSTANCE_CONFIG
"DEFAULT_OFFERING_USER_ATTRIBUTES": (
    ["username", "full_name"],
    "Default user attributes exposed to service providers.",
    "multiple_choice_field",
),

# In CONSTANCE_CONFIG_CHOICES
CONSTANCE_CONFIG_CHOICES = {
    "DEFAULT_OFFERING_USER_ATTRIBUTES": USER_ATTRIBUTE_CHOICES,
}
```

### 4. Dictionary Configuration
Use `dict_field` for complex settings. In the UI, this will be rendered with a JSON editor.

```python
"CUSTOM_MESSAGE_TEMPLATES": (
    {"welcome": "Hello {name}!"},
    "Custom templates for automated messages.",
    "dict_field",
),
```

## Where a value comes from

Each stored value records who wrote it last, in `Setting.source`, with `updated_by` and `source_detail` where they apply. A setting without a row reads as its default.

| Source | Written by |
|---|---|
| `api` | A person, through `POST /api/override-settings/` or another endpoint that changes settings. `updated_by` holds them. |
| `admin` | A person, through the Django admin settings page. `updated_by` holds them. |
| `deployment` | The deployment's configuration: `override_constance_settings` and `init_matrix_settings`, run by the Helm or Compose init job. `source_detail` holds the file or command. Re-applied on every deploy, which records an unchanged value as `deployment` too; a file applied with `--if-unset` (`languages.yaml`) only seeds settings without a value. |
| `cli` | An operator running a management command, such as `set_constance_image`, `set_login_logo_language` or `import_structure`. |
| `application` | Waldur itself, with no person acting, such as a refreshed eduTEAMS token or the telemetry deployment ID. |
| `migration` | A data migration, such as the copy out of django-constance in `configuration.0002`. |

The admin page and the API write only values that changed, so re-submitting a form does not claim the settings it did not change.

## Frontend Integration

Settings are automatically exported to the frontend via:
1.  **Metadata API**: `/api/metadata/settings/` provides the structured data.
2.  **Type Generation**: The `uv run waldur print_settings_description` command generates `SettingsDescription.ts` in HomePort, allowing for typed access and generic form rendering.

### Generic Rendering
The HomePort UI iterates through these settings and uses the `type` and `options` metadata to determine which component to render. The backend automatically merges `CONSTANCE_CONFIG` and `CONSTANCE_CONFIG_CHOICES` when exporting metadata.
