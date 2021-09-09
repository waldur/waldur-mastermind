# Invoices plugin

Invoice price is calculated based on its items. For each object that should be added to invoice (invoice item source) should be created a separate model.

Business logic for invoice item creation and registration should be covered in a registrator in the module `registrators.py`.

Invoice items creation and termination should be triggered in handlers that reacts on items sources deletion or save. `RegistrationManager` should be used in handlers.
