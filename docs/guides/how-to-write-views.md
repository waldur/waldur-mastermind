# How to write views

## View workflow

- **Filtering** - filter objects that are visible to a user based on his request.
  Raise 404 error if object is not visible.

- **Permissions check** - make sure that user has right to execute chosen action.
  Raise 403 error if user does not have enough permissions.

- **View validation** - check object state and make sure that selected action can be executed.
  Raise 409 error if action cannot be executed with current object state.

- **Serializer validation** - check that user's data is valid.

- **Action logic execution** - do anything that should be done to execute action.
  For example: schedule tasks with executors, run backend tasks, save data to DB.

- **Serialization and response output** - return serialized data as response.

## Pagination and ordering

List endpoints are sliced with `LIMIT/OFFSET`, so their ordering has to be a
*total* order: if the last sort key can repeat, rows sharing it may swap places
between two requests, and a page then shows one row twice while another is never
returned.

- Every model declares `Meta.ordering` ending on a unique column, normally the
  primary key: `ordering = ["-created", "id"]`. Declare `class Meta(Parent.Meta)`
  rather than a bare `class Meta` when inheriting, otherwise Django drops the
  parent's ordering. `PaginationOrderingTest` enforces both rules.
- `LinkHeaderPagination` appends the primary key to whatever ordering reaches it,
  which covers `?o=<field>` - an ordering filter *replaces* the ordering instead
  of extending it - and querysets built inside a custom action. Querysets built
  with `values()`, `union()` or `distinct(<field>)` are left untouched, since an
  extra column would change what they return.
- Widening an ordering changes `SELECT DISTINCT`: default-ordering columns are
  appended to the select list, so `values_list("x", flat=True).distinct()` starts
  deduplicating on the ordering columns as well. Call `order_by()` first to clear
  the ordering in aggregates of that shape.
