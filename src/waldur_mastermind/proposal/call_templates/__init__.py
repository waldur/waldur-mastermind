"""Call templates: call configurations a new call starts from.

Each ``<name>.json`` here is a document in the format ``call_transfer`` exports
and imports: importing one (the ``import_call`` action, or ``import_template``)
creates a draft call that a call manager then adapts -- rounds, requested
offerings and the people on the call are left to them. The evaluation forms
the steps use come with the template; each import makes the call its own copy.

The files are shipped with the call module as presets: a starting point for
a template. Staff load the ones a portal needs (``load_presets``, the
``load_preset`` action), which creates a ``CallTemplate`` from each, and
author further templates through the API. A template keeps no link to the
preset it was loaded from: once created it lives its own life. Call managers
create calls from the published templates.

A file may also carry, next to the document, the template's ``scenarios``
(named paths through its workflow); ``load`` leaves them out of the document
and ``load_scenarios`` returns them. A template loaded from a preset takes
them with the document, and its preset block as metadata.
"""

import json
from pathlib import Path

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from rest_framework import serializers

from waldur_core.structure import models as structure_models
from waldur_mastermind.proposal import (
    call_transfer,
    models,
    tripwires,
    workflow_versions,
)
from waldur_mastermind.proposal.enums import WorkflowPhases

DIRECTORY = Path(__file__).resolve().parent

# Document keys a template never carries, by the import section they belong to:
# rounds, requested offerings and documents are one call's own, run by one
# organisation. A template carries settings and workflow only.
EXCLUDED_KEYS = {
    "rounds": "rounds",
    "requested_offerings": "offerings",
    "documents": "documents",
}
EXCLUDED_SECTIONS = dict.fromkeys(EXCLUDED_KEYS.values(), False)

# The optional ``preset`` block of a template document: which organisation
# runs the process the template models, and where it is described. Metadata
# only: importing a document ignores it and exporting a call never emits it.
PRESET_KEY = "preset"
PRESET_CATEGORIES = ("generic", "programme")
PRESET_TEXT_FIELDS = {
    "organization": (True, 150),
    "process": (True, 150),
    "summary": (True, 300),
    "reference_title": (False, 200),
}
PRESET_URL_FIELDS = ("organization_url", "reference_url")
PRESET_URL_MAX_LENGTH = 500
PRESET_MAX_TAGS = 10
PRESET_TAG_MAX_LENGTH = 40
PRESET_FIELDS = {
    *PRESET_TEXT_FIELDS,
    *PRESET_URL_FIELDS,
    "category",
    "tags",
    "programme",
}
_validate_url = URLValidator(schemes=["http", "https"])

# The programmes presets belong to, by key: one file, so the facts of a
# programme several presets model -- who runs it, where it is described --
# are written once. A preset names its programme by key; a generic preset
# has none.
PROGRAMMES_FILE = "programmes.json"
PROGRAMME_TEXT_FIELDS = {
    "name": (True, 150),
    "organization": (True, 150),
    "summary": (False, 300),
}
PROGRAMME_FIELDS = {*PROGRAMME_TEXT_FIELDS, *PRESET_URL_FIELDS}


def _check_text(block, fields, errors):
    for key, (required, max_length) in fields.items():
        value = block.get(key)
        if value is None:
            if required:
                errors[key] = "This field is required."
        elif not isinstance(value, str) or (required and not value.strip()):
            errors[key] = "Expected a non-empty string."
        elif len(value) > max_length:
            errors[key] = f"Ensure this field has no more than {max_length} characters."


def _check_urls(block, errors):
    for key in PRESET_URL_FIELDS:
        value = block.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > PRESET_URL_MAX_LENGTH:
            errors[key] = (
                f"Expected a URL of at most {PRESET_URL_MAX_LENGTH} characters."
            )
            continue
        try:
            _validate_url(value)
        except DjangoValidationError:
            errors[key] = "Expected an http or https URL."


def _raise(prefix, errors):
    if errors:
        raise serializers.ValidationError(
            {
                f"{prefix}.{key}" if key else prefix: message
                for key, message in errors.items()
            }
        )


def check_programme(key, entry):
    """Refuse a programme entry of the wrong shape. Returns it unchanged."""
    prefix = f"programmes.{key}"
    if not isinstance(entry, dict):
        raise serializers.ValidationError({prefix: "Expected a mapping."})
    errors = {}
    unknown = sorted(set(entry) - PROGRAMME_FIELDS)
    if unknown:
        errors[""] = f"Unknown keys: {', '.join(unknown)}."
    _check_text(entry, PROGRAMME_TEXT_FIELDS, errors)
    _check_urls(entry, errors)
    _raise(prefix, errors)
    return entry


def programmes() -> dict[str, dict]:
    """The programmes shipped presets belong to, by key."""
    path = DIRECTORY / PROGRAMMES_FILE
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def check_preset(block):
    """Refuse a preset block of the wrong shape. Returns it unchanged.

    A generic block describes a pattern any organisation can adopt and
    needs no organisation. A block naming a programme belongs to the
    ``programme`` category and takes the programme's organisation unless it
    names one itself; any other programme block names its organisation.
    """
    if not isinstance(block, dict):
        raise serializers.ValidationError({PRESET_KEY: "Expected a mapping."})
    errors = {}
    unknown = sorted(set(block) - PRESET_FIELDS)
    if unknown:
        errors[""] = f"Unknown keys: {', '.join(unknown)}."
    programme = block.get("programme")
    text_fields = PRESET_TEXT_FIELDS
    if programme is not None or block.get("category") == "generic":
        text_fields = {**text_fields, "organization": (False, 150)}
    if programme is not None:
        if not isinstance(programme, str) or programme not in programmes():
            errors["programme"] = "Unknown programme."
        elif block.get("category") != "programme":
            errors["category"] = "A preset of a programme is in the programme category."
    _check_text(block, text_fields, errors)
    _check_urls(block, errors)
    if block.get("category") not in PRESET_CATEGORIES:
        errors["category"] = f"Expected one of: {', '.join(PRESET_CATEGORIES)}."
    tags = block.get("tags", [])
    if (
        not isinstance(tags, list)
        or len(tags) > PRESET_MAX_TAGS
        or not all(
            isinstance(tag, str) and 0 < len(tag.strip()) <= PRESET_TAG_MAX_LENGTH
            for tag in tags
        )
    ):
        errors["tags"] = (
            f"Expected at most {PRESET_MAX_TAGS} non-empty tags of at most "
            f"{PRESET_TAG_MAX_LENGTH} characters."
        )
    _raise(PRESET_KEY, errors)
    return block


def resolve_preset(block, known_programmes=None) -> dict:
    """``block`` as it is shown: its programme, if any, as the programme's
    entry with its key, and the programme's organisation where the block
    names none; a generic block may name no organisation (None). ``block``
    is a well-formed preset block."""
    key = block.get("programme")
    if key is None:
        return {
            **block,
            "organization": block.get("organization"),
            "organization_url": block.get("organization_url"),
            "programme": None,
        }
    if known_programmes is None:
        known_programmes = programmes()
    entry = known_programmes[key]
    programme = {
        "key": key,
        **{field: entry.get(field) for field in sorted(PROGRAMME_FIELDS)},
    }
    return {
        **block,
        "organization": block.get("organization") or programme["organization"],
        "organization_url": block.get("organization_url")
        or programme.get("organization_url"),
        "programme": programme,
    }


def names() -> list[str]:
    """Names of the shipped templates."""
    return sorted(
        path.stem for path in DIRECTORY.glob("*.json") if path.name != PROGRAMMES_FILE
    )


# Keys of a template file that are not part of its document: its checks.
CHECK_KEYS = ("scenarios",)


def _read(name: str) -> dict:
    if name not in names():
        raise KeyError(f"No call template named '{name}'.")
    return json.loads((DIRECTORY / f"{name}.json").read_text())


def load(name: str) -> dict:
    """The document of the template ``name``, without its preset block and
    scenarios.

    The preset block describes where the template comes from; it is not part
    of the call, and ``preset_block`` returns it. Scenarios are the
    template's checks, which ``load_scenarios`` returns.
    """
    document = _read(name)
    for key in (PRESET_KEY, *CHECK_KEYS):
        document.pop(key, None)
    return document


def load_scenarios(name: str) -> list:
    """The scenarios the template ``name`` ships with."""
    return list(_read(name).get("scenarios") or [])


def preset_block(name: str) -> dict | None:
    """The preset block of the shipped template ``name`` as the file has it;
    None without one."""
    try:
        return _read(name).get(PRESET_KEY)
    except KeyError:
        return None


def preset_info(name: str, known_programmes=None) -> dict | None:
    """The preset block of the shipped template ``name`` with its programme
    resolved (``resolve_preset``); None without one."""
    block = preset_block(name)
    if block is None:
        return None
    return resolve_preset(block, known_programmes)


def import_template(name, manager, user, call_name=None):
    """Create a draft call under ``manager`` from the template ``name``.

    Returns ``(call, imported_sections, warnings)``, as ``import_call`` does.
    """
    return call_transfer.import_call(load(name), manager, user, name=call_name)


def preset_name(name, document=None) -> str:
    """What the preset ``name`` is called: its call's name, else its key."""
    if document is None:
        try:
            document = load(name)
        except KeyError:
            return name
    call = document.get("call") or {}
    return call.get("name") or name


def presets() -> list[dict]:
    """The shipped templates as presets, with whether a template already
    holds the name a preset would be loaded under."""
    taken = set(models.CallTemplate.objects.values_list("name", flat=True))
    result = []
    known = programmes()
    for name in names():
        document = load(name)
        call = document.get("call") or {}
        result.append(
            {
                "key": name,
                "name": preset_name(name, document),
                "description": call.get("description") or "",
                "steps": step_summary(document),
                "preset": preset_info(name, known),
                "name_taken": template_name(name, document) in taken,
            }
        )
    return result


def step_summary(document) -> list[dict]:
    """The workflow steps of ``document``: name, kind, order and whether enabled."""
    steps = document.get("workflow_steps") if isinstance(document, dict) else None
    if not isinstance(steps, list):
        return []
    summary = [
        {
            "name": step.get("name") or step.get("key") or "",
            "kind": step.get("kind"),
            "order": step.get("order"),
            "is_enabled": bool(step.get("is_enabled", True)),
            "phase": step.get("phase") or WorkflowPhases.EVALUATION,
        }
        for step in steps
        if isinstance(step, dict)
    ]
    return sorted(
        summary,
        key=lambda s: (not isinstance(s["order"], int), s["order"] or 0),
    )


def check_document(document):
    """Refuse a document a template may not hold.

    Its schema version must be one this portal imports, it may carry no
    rounds, requested offerings or documents, and its preset block, if any,
    must have the expected shape.
    """
    version = document.get("schema_version")
    if version not in call_transfer.SUPPORTED_SCHEMA_VERSIONS:
        raise serializers.ValidationError(f"Unsupported schema version: {version!r}.")
    if document.get(PRESET_KEY) is not None:
        check_preset(document[PRESET_KEY])
    for key in EXCLUDED_KEYS:
        if document.get(key):
            raise serializers.ValidationError(
                f"A call template carries the call's settings and workflow only; "
                f"remove the '{key}' section."
            )
    return document


class _DryRunDone(Exception):
    def __init__(self, warnings):
        super().__init__()
        self.warnings = warnings


def dry_run(document, user) -> list[str]:
    """Import ``document`` into a throwaway organisation and undo it.

    Returns the import's warnings; an invalid document raises the import's
    ValidationError. Nothing the import creates survives: it runs in a
    savepoint that is always rolled back. The organisation owns no forms, so
    the warnings are those of an organisation using the portal's shared forms
    only.

    The organisation and the import are scaffolding: no handler announces
    them, and any side effect they reach fails the check rather than
    leaving the savepoint.
    """
    try:
        with transaction.atomic(), tripwires.sandboxed():
            customer = structure_models.Customer.objects.create(
                name="Call template check"
            )
            manager = models.CallManagingOrganisation.objects.create(customer=customer)
            _, _, warnings = call_transfer.import_call(
                document, manager, user, sections=EXCLUDED_SECTIONS
            )
            raise _DryRunDone(warnings)
    except _DryRunDone as done:
        return done.warnings


NAME_TAKEN = "A call template with this name already exists."


def save_unique(save):
    """Run ``save`` in a savepoint. Validation checks that a name is free, but
    a concurrent save can take it first; the database then refuses the row,
    and that is reported as the field error validation would have given."""
    try:
        with transaction.atomic():
            return save()
    except IntegrityError as e:
        if "name" not in str(e):
            raise
        raise serializers.ValidationError({"name": [NAME_TAKEN]})


def template_name(name, document=None) -> str:
    """The name a template loaded from the preset ``name`` takes."""
    return preset_name(name, document)[:150]


def name_taken_message(template) -> str:
    return (
        f"A call template named '{template.name}' already exists. Rename or "
        "delete it first to load this preset again."
    )


def create_from_preset(name, publish=False):
    """Create a ``CallTemplate`` from the preset ``name``: a copy of its
    document and scenarios, with its preset block as metadata, published only
    if ``publish``. The template keeps no link to the preset.

    Refused when a template already holds the name it would take.
    """
    document = {**load(name), PRESET_KEY: preset_block(name)}
    if document[PRESET_KEY] is None:
        document.pop(PRESET_KEY)
    call = document.get("call") or {}
    template_name_ = template_name(name, document)
    existing = models.CallTemplate.objects.filter(name=template_name_).first()
    if existing is not None:
        raise serializers.ValidationError({"presets": [name_taken_message(existing)]})
    template = save_unique(
        lambda: models.CallTemplate.objects.create(
            name=template_name_,
            description=call.get("description") or "",
            document=document,
            schema_version=document["schema_version"],
            is_published=publish,
            scenarios=load_scenarios(name),
        )
    )
    workflow_versions.record(template, reason=workflow_versions.Reasons.CREATED)
    return template


@transaction.atomic
def load_presets(keys, publish=False, skip_existing=False) -> dict[str, list]:
    """Create a template from each preset in ``keys``.

    A preset whose name a template already holds is refused, the whole load
    with it, unless ``skip_existing``: then it is skipped. Returns the names
    of the presets created and skipped.
    """
    created, skipped = [], []
    for name in dict.fromkeys(keys):
        if (
            skip_existing
            and models.CallTemplate.objects.filter(name=template_name(name)).exists()
        ):
            skipped.append(preset_name(name))
            continue
        create_from_preset(name, publish)
        created.append(preset_name(name))
    return {"created": created, "skipped": skipped}
