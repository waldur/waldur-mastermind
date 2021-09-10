# Add a new language for translatable models

For translating fields of some models we use
[django modeltranslation](<https://django-modeltranslation.readthedocs.io/en/latest/>).

## First run

To setup the database environment, after completing all migrations, execute in a console:

```bash
waldur update_translation_fields
```

## Add a new language

To populate the generated language tables with initial content, run

```bash
waldur sync_translation_fields
```
