# Listing permissions

Entities of Waldur are grouped into *organisational units*. The
following *organisational units* are supported: customer and project.

Each *organisational unit* has a list of users associated with it.
Getting a list of users connected to a certain *organisational unit* is
done through running a GET request against a corresponding endpoint.

- customer: endpoint `/api/customer-permissions/`
- project: endpoint `/api/project-permissions/`

Filtering by *organisational unit* UUID or URL is supported. Depending
on the type, filter field is one of:

- `?customer=<UUID>`
- `?customer_url=<URL>`
- `?project=<UUID>`
- `?project_url=<URL>`
- `?user_url=<URL>`

In addition, filtering by field names is supported. In all cases
filtering is based on case insensitive partial matching.

- `?username=<username>`
- `?full_name=<full name>`
- `?native_name=<native name>`

Ordering can be done by setting an ordering field with
`?o=<field_name>`. For descending ordering prefix field name with a
dash (-). Supported field names are:

- `?o=user__username`
- `?o=user__full_name`
- `?o=user__native_name`
