# Media Access Control

Every `FileField` and `ImageField` in Waldur is stored by `DatabaseStorage`, the
global default storage. Uploads do not go to disk or S3 — they become rows in
`waldur_core.media.models.File`, and they are all served by a single endpoint:

```text
GET /api/media/<uuid>/
```

That endpoint has `permission_classes = ()`. The only thing it knows about a
file is its storage path, so **access rules are keyed by the `upload_to`
prefix**, and they are **deny by default**: a file whose prefix has no
registered rule is served to nobody, staff included.

## Adding a file field

Whenever you add a `FileField` or `ImageField`, you must also declare who may
download it. Skipping this step is not silently insecure — it is silently
*broken*, and `CoverageTest` in `waldur_core/media/tests/test_access_registry.py`
fails until you do.

Declare rules in a `media_access.py` module at the root of your app. It is
imported automatically at startup; no registration in `apps.py` is needed.

```python
# src/waldur_mastermind/invoices/media_access.py
"""Media access rules for files owned by the invoices app.

See :mod:`waldur_core.media.access`.
"""

from waldur_core.media import access
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.invoices.models import Payment

# Payment.Permissions routes through profile__organization, which is what
# PaymentViewSet's GenericRoleFilter applies.
access.register(
    access.upload_prefix(Payment, "proof"),
    access.queryset_rule(Payment, ["proof"], filter_queryset_for_user),
)
```

That is the real rule for payment proofs, verbatim.

## Choosing a rule

| Helper | Who gets the file |
|---|---|
| `register_public(prefix)` | Everyone, including anonymous users |
| `register_authenticated(prefix)` | Any logged-in user, no per-object check |
| `register_staff_only(prefix)` | Staff and support only |
| `register(prefix, check)` | Custom `check(file, user) -> bool` |
| `queryset_rule(...)` | Whoever the app's own read rule allows |

Prefer `queryset_rule`. It looks the file up in its owning table and hands the
result to the same filter the ViewSet uses — usually
`filter_queryset_for_user` or a manager's `filter_for_user` — so a download
cannot outlive the permission the API itself enforces.

Use `register_public` **only** for files already reachable through an anonymous
API endpoint, and say which one in a comment. Everything on the unauthenticated
marketplace catalogue and the public call-for-proposals page falls in this
category.

## Deriving the prefix

Never write the prefix as a literal. Derive it, so renaming an `upload_to`
cannot leave a stale rule registered and the new path wide open:

- `access.upload_prefix(Model, "field")` — for a string `upload_to`. A
  strftime-formatted value such as `matrix_exports/%Y/%m/` is truncated at the
  first format token.
- `access.image_prefix(Model)` — for a field using `get_upload_path` from
  `waldur_core.media.mixins`, including anything using `ImageModelMixin`.

`get_upload_path` builds paths from the bare model name with no app label, so
two image-bearing models with the same class name in different apps would share
a prefix. `test_model_names_used_as_prefixes_are_unique` guards against that.

## Constraints on `media_access` modules

`MediaConfig.ready()` runs before `constance` and before the plugin apps' own
`ready()`. A `media_access` module therefore must not:

- touch the database or `constance.config` at import time;
- import its own app's `views` or `serializers`.

Building a closure is fine; evaluating one is not. If you need a queryset
helper that currently lives in `views.py`, move it to `managers.py` — see
`matrix_chat.managers.get_accessible_room_ids`.

A `media_access` module that fails to import raises rather than warning. Under
default-deny a swallowed import error would silently stop serving every file
that app owns.

## Out-of-tree plugins

Waldur loads any plugin registered under the `waldur_extensions` entry point,
including packages outside this repository. `CoverageTest` only walks the
models of installed apps in CI, so it cannot warn a third-party plugin author.

**If you maintain a Waldur plugin with a `FileField` and it is not in this
repository, add a `media_access.py` to it.** Without one, its files return 404
for every user after upgrading. The failure is fail-closed rather than a data
leak, but it is still an outage for that plugin's downloads.

## Testing

For a test that needs a synthetic registry, use the context manager — never
clear the registry directly. Autodiscovery runs once per process, so an
unrestored registry would leave every later test in the same worker denying
everything:

```python
with access.override_rules():
    access.register_public("my_test_prefix/")
    ...
```

## Reference

- `src/waldur_core/media/access.py` — the registry
- `src/waldur_core/media/apps.py` — autodiscovery
- `src/waldur_core/media/tests/test_access_registry.py` — registry and coverage tests
- `src/waldur_mastermind/marketplace/media_access.py` — a worked example covering
  public, authenticated and scope-checked prefixes
