# Adding Sections to Marketplace Categories

## Information

Waldur marketplace categories can have **sections** (like "Support", "Security", "Location") that contain **attributes** (like "E-mail", "Phone", "Support portal"). These metadata fields appear when editing offerings under **Public Information → Category** in the UI.

- Categories created via `load_categories` command → Have sections automatically
- Categories created manually via UI/API → No sections by default

## Quick Start: Adding Support Section Example

### Step 1: Check which categories need Support

Open Django shell:

```bash
waldur shell
```

To see all categories without a Support section:

```python
from waldur_mastermind.marketplace.models import Attribute, Category
categories = Category.objects.all()

for category in categories:
    has_support = category.sections.filter(
        key__icontains="support"
    ).exists() or category.sections.filter(
        title__iexact="support"
    ).exists()

    if not has_support:
        offerings_count = category.offerings.count()
        print(f"• {category.title} (UUID: {category.uuid}, {offerings_count} offerings)")
```

### Step 2: Define the helper function

Define the function to add Support section:

```python
from waldur_mastermind.marketplace.models import Attribute, Category, Section

SUPPORT_SECTION_ATTRIBUTES = [
    ("email", "E-mail", "string"),
    ("phone", "Phone", "string"),
    ("portal", "Support portal", "string"),
    ("description", "Description", "string"),
]

def add_support_section_to_category(category_identifier, section_key_prefix=None):
    """Add Support section with standard attributes to a category."""
    try:
        category = Category.objects.get(uuid=category_identifier)
    except (ValueError, Category.DoesNotExist):
        try:
            category = Category.objects.get(title=category_identifier)
        except Category.DoesNotExist:
            print(f"Category '{category_identifier}' not found!")
            return None, 0

    if section_key_prefix is None:
        section_key_prefix = category.title.lower().replace(" ", "_").replace("-", "_")

    section_key = f"{section_key_prefix}_Support"

    existing_section = Section.objects.filter(
        key=section_key,
        category=category
    ).first()

    if existing_section:
        print(f"  → Support section already exists (key: {section_key})")
        section = existing_section
    else:
        section = Section.objects.create(
            key=section_key,
            title="Support",
            category=category,
            is_standalone=True
        )
        print(f"Created Support section (key: {section_key})")

    attributes_created = 0
    for attr_key, attr_title, attr_type in SUPPORT_SECTION_ATTRIBUTES:
        full_key = f"{section_key}_{attr_key}"

        attribute, created = Attribute.objects.get_or_create(
            key=full_key,
            defaults={
                "title": attr_title,
                "type": attr_type,
                "section": section,
            }
        )

        if created:
            attributes_created += 1
            print(f"Created attribute: {attr_title}")
        else:
            print(f"Attribute already exists: {attr_title}")

    print(f"Summary: {attributes_created} new attribute(s) created")
    return section, attributes_created
```

### Step 3: Use the function

Add Support section to your categories:

```python
# Single category by name
add_support_section_to_category("Applications")

# Or by UUID
add_support_section_to_category("category-uuid-here")

# Multiple categories
for category_name in ["Applications", "Application Support", "Consultancy and Expertise"]:
    add_support_section_to_category(category_name)
```

## What Gets Created

The Support section includes these attributes:

| Attribute | Type | Description |
|-----------|------|-------------|
| E-mail | string | Support contact email |
| Phone | string | Support phone number |
| Support portal | string | URL to support portal |
| Description | string | General support description |

### Section Keys and Naming

Keys are automatically generated based on category title:

- Category: `"Applications"` → Section key: `applications_Support`
- Category: `"Application Support"` → Section key: `application_support_Support`

Attribute keys follow the pattern: `{section_key}_{attribute_name}`

**Example for "Applications" category:**

- `applications_Support_email`
- `applications_Support_phone`
- `applications_Support_portal`
- `applications_Support_description`

## Adding Other Sections

You can add other types of sections (Security, Location, etc.) using similar pattern.

## Available Attribute Types

| Type | UI Element | Use Case |
|------|------------|----------|
| `string` | Text input | Short text (emails, names, URLs) |
| `text` | Textarea | Long text (descriptions) |
| `integer` | Number input | Numeric values |
| `boolean` | Checkbox | Yes/No values |
| `choice` | Dropdown | Single selection from list |
| `list` | Multi-select | Multiple selections from list |

**Note:** For `choice` and `list` types, you need to create `AttributeOption` objects:

## Verification

After adding sections, verify in the UI:

1. Log into Waldur
2. Navigate to **Marketplace → Offerings**
3. Edit an offering in the category you modified
4. Go to **Public Information → Category**
5. You should see the new section and attributes

## Reference

For more examples, see the standard category definitions in:

- `src/waldur_mastermind/marketplace/management/commands/load_categories.py`

