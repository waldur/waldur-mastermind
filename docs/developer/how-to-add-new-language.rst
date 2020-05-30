Adding a new language translatable models
=========================================

For translateing fields of some models we use
`django modeltranslation <https://django-modeltranslation.readthedocs.io/en/latest/>`_.

First run
---------

To setup the database environment, after completing all migrations, execute in
the console 'waldur update_translation_fields'.


Add new language
----------------

To populate the generated language tables with initial content, run
 'waldur sync_translation_fields'.
