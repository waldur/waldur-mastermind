# How to Reconcile a Long-Diverged Fork

This guide distills what we learned while porting Isambard's OpenPortal fork
(diverged from `develop` for ~9 months, ~600 commits) back into
`waldur_openportal`. The same failure patterns will recur for any similarly
long-lived fork (a customer deployment, a vendor integration, a proof-of-concept
branch) — use this as a checklist before merging, and while triaging what breaks
afterward.

The core risk in every step below is the same one: **a fork's version of a file
is not a superset of develop's version.** It is an independently-evolved
sibling. Reconciling by replacing develop's file with the fork's (or vice versa)
silently deletes whichever side's work isn't present in the other. Nothing
raises an exception when this happens — the code still runs, imports still
succeed, and the gap only shows up later as a missing feature, a noisy log, or
a schema that lies about the response shape.

## 1. Before merging: diff by feature, not by file

Don't diff the fork against develop and eyeball the result. For each file the
merge touches, diff **develop's pre-fork version against the merged result**
too. That second diff answers "what did develop have that just vanished?" —
which the first diff can't tell you, since a deletion looks identical to
"never existed."

Concretely, for a file `F` that both branches touched since the fork point:

```bash
git diff <merge-base> develop -- F   # what did develop add since the fork?
git diff <merge-base> fork/branch -- F   # what did the fork add?
git diff <post-merge-commit> develop -- F  # what's now missing that develop had?
```

Anything in the third diff that isn't in the second is a silent regression.

### What to check specifically

Not everything shows up as a functional bug — most of what we found were
things that still *run* but no longer *document themselves correctly* or no
longer *behave efficiently*. Check these categories explicitly, because "the
tests still pass" won't catch them:

- **Custom `@extend_schema_field`-wrapped fields and nested serializers.**
  A field that used to be `ProjectUsageReportField()` (giving drf-spectacular a
  real nested type) silently becoming a bare `JSONField()` is not visible in a
  test unless the test asserts on the generated schema. See §4.
- **`@extend_schema` parameters/responses on views.** An endpoint that used to
  document its query params and response shape can end up `exclude=True`, or
  lose its `parameters=`/`responses=` entirely, if the fork's version of the
  same endpoint never had that documentation.
- **Utility functions that only exist on develop.** If develop added
  `invite_user_to_project()` as a deliberate fix and the fork's parallel
  history never had it, a wholesale file swap deletes the function outright.
  grep the pre-merge file for `def` names and diff that list against the
  post-merge file.
- **Guard clauses and logging that only exist on develop.** We found 9
  scheduled Celery tasks that used to short-circuit with
  `if not config.ensure_config_loaded(): logger.debug(...); return` before
  doing any work. After the port, only 2 of the 9 still had it — the other 7
  ran their full body (including DB queries) on every tick even when the
  integration was completely disabled, and logged spurious `ERROR`s instead of
  a quiet `DEBUG` skip.
- **Correctness fixes that look like unrelated formatting.** A `.select_related("project")` that prevents N+1 queries is easy to lose in a
  merge because it doesn't change behavior in a way any test asserts on —
  only query count does.
- **`request=None` / other schema-only annotations.** These exist purely to
  tell drf-spectacular "this action takes no body." Dropping them doesn't
  break the endpoint, but if your repo has a lint gate for this
  (`scripts/check_action_request_none.py` — run it in `--fix` mode), it will
  fail CI on the merge, not on either parent branch individually.

## 2. After merging: run the full test suite and read every failure

Do not treat a batch of failing tests as noise to skip past. In this port, 53
tests failed, and **every one of them was a real signal** — not one was "the
test is just outdated for no reason." They broke down into:

| Cause | Fix |
|---|---|
| Test mocks a name that got renamed during the port (`openportal_config` → `config`) | Update the mock target string |
| Test asserts an error message that legitimately changed for the better | Update the test to the new (better) string, after confirming it's actually better |
| Test asserts synchronous behavior that was deliberately parallelized (`sync_remote_usage` became a fan-out dispatcher) | Rewrite the test for the new, correct architecture — don't revert the architecture |
| Test asserts exact call *order* against code with an intentional `random.shuffle` | Assert membership (`assert_has_calls(..., any_order=True)`), not order |
| Function the test calls no longer exists | This is the interesting case — see below |

**When a test references a function/attribute that's gone, don't assume the
test is stale.** Check `git log -p` on the file for a rename, and check
whether anything currently calls the *replacement* behavior:

- `utils.invite_user_to_project` had vanished entirely. It wasn't renamed —
  the port's design replaced invitation-based membership with direct role
  grants (`set_project_member_role`) for a *different* code path (remote/
  managed-project sync). But the original, tested, local-invite flow was a
  real develop feature with no fork equivalent, and nothing else had taken
  over its job. We restored the original function rather than deleting its
  test, but did **not** rewire the managed-project sync path to use it — that
  would have been a behavior change (require-acceptance vs. auto-add) well
  outside what "fix the tests" authorizes. When you hit this shape of
  ambiguity, restore the missing piece but stop at the boundary of the
  original request; flag the larger design question instead of resolving it
  unilaterally.
- `tasks.notify_users_about_rejected_allocation` had vanished too. The
  replacement code (`project.notify_rejected()`) does something adjacent but
  different — it notifies the *remote* OpenPortal side, not local Waldur
  admins/managers by email. These aren't mutually exclusive; we added the
  missing call back in *alongside* the existing one, not instead of it.

The general rule: **a missing function referenced only by a test is a
regression to investigate, not a test to delete.** Confirm what currently
happens at that code path, decide whether the old and new behaviors are
alternatives or complements, and only then decide the fix.

## 3. Migration graphs: assume the fork has its own numbering

If the fork maintained its own Django migrations for ~months, assume its
migration history both duplicates and diverges from develop's, and verify
before you trust `makemigrations`/`migrate` to just work:

- **Check for duplicate leaf nodes per app**, without touching a database:

  ```python
  from django.db.migrations.loader import MigrationLoader
  loader = MigrationLoader(None, ignore_no_migrations=True)
  [k for k in loader.graph.leaf_nodes() if k[0] == "your_app"]
  ```

  More than one leaf for an app means two branches never got reconciled — a
  fresh database will try to apply both, and if they created the same model
  (which is exactly what happened here — two migrations independently created
  `CachedProjectUsageReport`/`CachedProjectStorageReport` with identical
  fields and indexes), the second `CREATE TABLE` hard-fails.
- **Check cross-app dependencies for migrations that don't exist in your
  tree.** A migration ported from the fork can declare
  `dependencies = [("structure", "0067_merge_20251110_0435")]` referencing a
  migration that only ever existed in the fork's own `structure` app history.
  This raises `NodeNotFoundError` before Django even reaches the database.
  Fix by repointing at your actual latest migration for that app — Django
  doesn't require the *minimal* correct dependency, just a valid, already-
  ordered one.
- **When two branches turn out to create the same models identically** (same
  fields, same generated index names — Django/Postgres index names are
  deterministic from table+column, so identical models produce identical
  auto-generated names, which is itself a strong signal they're duplicates),
  delete the branch that was never deployed and repoint downstream migrations
  at the branch that was. Never edit or renumber a migration that's already
  been applied anywhere real — check by looking for it in the pre-port base
  branch; if it's there, deployments have already recorded it as applied.
- **Verify with a real `migrate`, not just your test suite.** If your project
  runs pytest with `--no-migrations` (check `pytest.ini`/`pyproject.toml`),
  your test suite never walks the actual migration graph — it builds schema
  straight from current models. That's exactly why this class of bug survives
  a fully-green local test run and only surfaces in CI (or on a fresh
  deployment). Verify with an actual `migrate` against a scratch Postgres
  database before trusting the fix:

  ```bash
  psql -c "CREATE DATABASE scratch_migration_check;"
  DJANGO_SETTINGS_MODULE=... python -c "
  import django; django.setup()
  from django.conf import settings
  settings.DATABASES['default']['NAME'] = 'scratch_migration_check'
  from django.core.management import call_command
  call_command('migrate')
  "
  psql -c "DROP DATABASE scratch_migration_check;"
  ```

## 4. drf-spectacular gotchas that surface specifically on a merge

These are generic drf-spectacular issues, but a fork merge is exactly when
they get triggered — usually because a previously-excluded or previously
default-configured endpoint becomes schema-visible for the first time, or
because two independently-written serializers use equivalent but distinct
choice definitions.

- **`ReadOnlyField(source="get_FOO_display")` can't be type-hinted.** Django's
  auto-generated `get_FOO_display()` method has no usable type hint, so
  drf-spectacular warns `unable to resolve type hint for function "_method"`.
  Fix: declare the field explicitly as `CharField(source="get_FOO_display",
  read_only=True)` — it's always a string.
- **A custom `OpenApiAuthenticationExtension` that subclasses a built-in
  scheme (e.g. `TokenScheme`) inherits that scheme's `priority` and
  `match_subclasses`.** If you don't override `priority`, your extension ties
  with the built-in one on every subclass of the base auth class, and loses
  the tie-break (stable sort favors whichever registered first — usually the
  built-in). Symptom: `Encountered 2 components with identical names
  "tokenAuth"...`. Fix: set an explicit `priority = 0` (or higher) on your
  extension.
- **A stale `target_class` string rots silently.** `OpenApiAuthenticationExtension.target_class` is a dotted string resolved lazily; if the
  class it points at gets renamed, the extension just stops matching anything
  — no error, no warning, until something else starts using the *default*
  scheme name for that class and collides with a different endpoint. When you
  see an unexplained naming collision involving one of your own auth classes,
  check `git log -p` for a rename before assuming the collision is new.
- **Two `ChoiceField`s with equivalent-but-distinct choice values collide.**
  If one serializer does `choices=["open", "members_only", ...]` (a literal)
  and another does `choices=SomeChoices.CHOICES` (a shared constant) for the
  same conceptual enum, drf-spectacular treats them as different components
  and auto-suffixes one (`FooEnum98a`) with a warning. Fix: always reference
  the shared choices constant, never duplicate the literal.
- **`raise JsonResponse(...)` is a silent footgun.** `JsonResponse` is not an
  exception; `raise`-ing one crashes with `TypeError: exceptions must derive
  from BaseException` instead of returning the intended error response. This
  is an easy typo to introduce when refactoring `return` statements under
  time pressure — grep for `raise JsonResponse` and `raise HttpResponse` after
  any large mechanical edit.
- **`JsonResponse({"error": ...})` without `status=` defaults to 200.** Every
  error branch should set an explicit status code; a bare `JsonResponse` on an
  error path will report success to any caller checking `response.ok`.

## 5. Use the fork's own generated artifacts as ground truth

If the fork's underlying system is written in Rust (or anything with its own
codegen) and generates TypeScript bindings for a frontend
(`ts-rs`/`typeshare`/similar), those bindings are a more reliable source of
truth for wire-format shapes than either side's hand-written Python
serializers. We used
`waldur-homeport/src/openportal/bindings/*.ts` to build out fully-typed
`AwardDetailsSerializer`/`LinkSerializer`/`NoteSerializer` — including catching
that the JSON key is `template`, not `project_template` (the Rust struct's
Python-exposed property name differs from its `serde` JSON key, which the
TypeScript binding reveals and the Python `.pyi` stub does not).

Concretely: for every `JSONField`/`DictField(allow_null=True)`/opaque
`SerializerMethodField` your serializers use to represent fork-defined data,
check whether a `.ts` binding exists for that shape, and if so, mirror it
field-for-field (including nullability and which fields are actually optional
— `?` in the binding vs. `| null`) rather than leaving it untyped. Verify the
binding still matches reality by actually constructing the object and calling
`.to_json()` — codegen and hand-maintained stubs both drift.

## 6. Don't trust a `.pyi` stub (or a type checker) over runtime reality

We hit a case where a static type checker (Pyrefly) reported `No attribute
'ProjectDetails' in module 'openportal'` — correct, per the stub — but the
attribute worked fine at runtime: it's an undocumented alias for
`AwardDetails` that the packaged `.pyi` simply doesn't declare. Before
"fixing" a reported missing-attribute error by guessing a replacement, check
what the object actually is at runtime:

```python
import openportal
print(openportal.ProjectDetails, type(openportal.ProjectDetails))
# <class 'openportal.AwardDetails'> <class 'type'>
```

We still switched to the stub-documented name (`AwardDetails`) everywhere,
because relying on an undocumented alias that isn't in the stub is fragile —
it could be dropped in a future release, and every type checker will keep
flagging it. But the fix followed from confirming the alias really was just an
alias, not from assuming the type checker was right about something being
broken.

## 7. A matching API surface does not mean matching behaviour

Section 6 is about the stub disagreeing with runtime. This is the inverse, and
it bites harder: the surface can be exactly what you expect while the behaviour
underneath is not. Three cases in `waldur_openportal` are load-bearing enough
that removing the workaround silently breaks something. Each was found by
running the packaged wheel, never by reading code or stubs — the SDK is a
compiled Rust extension, so there is no source to read.

Confirmed still present in **openportal 0.91.0**. Before deleting any of these
workarounds, re-run the probe against the version you are on and paste the
result into the commit message.

### 7.1 `OpenPortalUnsupportedCommandError` does not exist

The port referenced `openportal.OpenPortalUnsupportedCommandError` in four
places, as the older-portal fallback for `get_award`. The SDK has never defined
it:

```python
>>> import openportal; hasattr(openportal, "OpenPortalUnsupportedCommandError")
False
```

An `except` clause is only evaluated when something is raised, so this looks
fine until the day the fallback is actually needed — at which point the
`except` raises `AttributeError` *while handling* the original error, and the
fallback that exists to keep older portals working is what breaks the request.

The plugin defines the exception itself, in `waldur_openportal/exceptions.py`
alongside the other plugin errors, and `board.refetch_award` raises it after
matching on the remote's `"Unknown command"` message. If a future SDK adds a
real one, switch to it — but check with the probe above, don't assume.

### 7.2 The `allowed_domains` setter turns `[]` into `None`

For this field the two values mean opposite things: `None` is "no restriction",
`[]` is "nothing allowed". Assignment cannot express the second:

```python
d = AwardDetails.from_json('{"allowed_domains": ["*.ac.uk"]}')
d.allowed_domains = []
d.allowed_domains          # -> None,  not []
```

`from_json` and `merge` both preserve `[]` — it is only the attribute setter
that collapses it. So any code that computes a restriction and assigns it turns
a lockdown into an open door, and does so quietly.

`RemoteProject.award_details()` therefore clears the field and merges the value
in rather than assigning it. Order matters: `merge` only fills a field that is
unset on the receiver, so the clear has to come first.

```python
result.allowed_domains = None
if self.allowed_domains is not None:
    result = result.merge(
        openportal.AwardDetails(json.dumps({"allowed_domains": self.allowed_domains}))
    )
```

Two related habits follow: never guard these fields with truthiness (`if x:`
treats `[]` as absent — use `is not None`), and assume the same hazard applies
to any other list-valued `AwardDetails` field until probed.

### 7.3 `Link` has no `set_url`

The fork's `ManagedProject.set_project_link` called `link.set_url(...)` inside
a bare `except Exception: pass`. The method does not exist:

```python
>>> hasattr(openportal.Link(), "set_url")
False
```

So every call raised `AttributeError`, the bare `except` swallowed it, and the
method's only effect was to store a `Link` with no URL. It had no callers and
duplicated `_get_project_link` with different semantics, so it was deleted
rather than fixed. `Link.url` is a plain attribute — assign it directly.

The general point: a bare `except` around a single SDK call will hide a
misspelled method for as long as the code lives. Catch the exception you
expect, or none.

## 8. Practical workflow notes

- **Cherry-pick a large fork in themed batches**, oldest-first within a theme,
  not as one wholesale merge commit. It's far easier to reason about "does
  this batch of notification-related commits conflict with develop's own
  notification work" than "does this 600-commit fork conflict with 9 months
  of develop." Expect conflicts to cluster around whichever area both sides
  were actively developing in parallel — that's a signal to slow down and
  read both sides' intent, not just resolve syntactically.
- **When cherry-picking hits a conflict that looks huge, check whether it's a
  real conflict or a diff-alignment artifact.** We hit a case where a
  cherry-pick conflict appeared to duplicate an entire unrelated function
  (`mark_stale_remote_projects`) that didn't actually exist yet on our side —
  git's 3-way diff had merely misaligned context around a real, tiny, one-line
  change elsewhere in the same file. Read the actual diff of the commit in
  isolation (`git show <commit> -- <file>`) before trusting the conflict
  markers' framing of what changed.
- **Verify against the exact CI command, locally, before pushing** — not an
  approximation of it. `waldur spectacular --validate` and `waldur spectacular
  --fail-on-warn` can disagree; run the one CI actually runs.
- **When a fix is scoped outside the module you're porting** (e.g. a
  `waldur_core` authentication schema bug that a fork's endpoint happens to be
  the first thing to trigger), verify it's really pre-existing before
  assuming it's in scope — check out the pre-fork base branch in an isolated
  `git worktree` and re-run the same check there. If it passes on the base
  branch, your PR is what activates the bug, and the shared-file fix belongs
  in your PR regardless of which file it touches.
