"""Export a call's configuration to a portable document and import it elsewhere.

The document is a template, not a data dump: it carries the call's settings and
the configuration hanging off it, never proposals, reviews, reviewer pools,
assignments or anything naming a user. Nothing in it is identified by UUID --
offerings, plans, checklists and roles are referenced by name, because the
portal importing it has its own identifiers for all of them.

The evaluation forms of the workflow steps are the exception to naming: their
questions travel with the document, and an import gives the call its own copy
of each, so a form is usable on a portal that has never seen it and editing it
later does not change another call's form.

Plain (non-relational) model fields are copied generically, so a field added to
one of these models later travels without touching this module. Relations are
translated explicitly, and anything not listed here is left out.
"""

import base64
import datetime
import decimal
import itertools
import os
import re
import uuid
from collections import Counter

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Prefetch, Q
from rest_framework import serializers

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist import serializers as checklist_serializers
from waldur_core.permissions import models as permissions_models
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import (
    budgets,
    enums,
    models,
    notification_overrides,
    proposal_roles,
    utils,
)
from waldur_mastermind.proposal.enums import (
    DEFAULT_CALL_STEPS,
    DEFAULT_CALL_STEPS_MAP,
    CallStates,
    OrderAuthors,
    RequestedOfferingStates,
)

# 2: workflow steps carry a key, kind, name and order instead of naming one of
# the fixed catalogue steps; the call's evaluator roles travel with them, and
# steps and notification rules name those roles by key.
# 3: the evaluation forms the steps use travel as definitions -- questions,
# options and dependencies -- under "checklists"; a step still names its form.
SCHEMA_VERSION = 3
SUPPORTED_SCHEMA_VERSIONS = (1, 2, SCHEMA_VERSION)

# Sections a caller can leave out of an export or an import. Call settings are
# not a section: without them there is nothing to import into.
SECTIONS = (
    "documents",
    "rounds",
    "offerings",
    "workflow_steps",
    "field_configs",
    "review_configs",
    "role_mappings",
    "compliance_checklist",
)

# Identity, timestamps and portal-local state never travel.
_ALWAYS_EXCLUDED = frozenset(
    {"id", "uuid", "created", "modified", "slug", "backend_id"}
)

# Per-model plain fields that are portal-local or owned by someone other than
# the call manager, on top of _ALWAYS_EXCLUDED.
# Where the call came from is this portal's record, not its configuration. The
# logo is a stored file: its path means nothing on another portal, and taken
# from a document it would point the call at whatever file that path names.
_CALL_EXCLUDED = frozenset({"state", "template_version", "image"})
# Call.backend_id is not an external system's id but the reference code a call
# manager types in (it prefixes proposal names). It travels under the name the
# API and the UI give it.
_CALL_REFERENCE_CODE = "reference_code"
# The provider sets require_purchase_order; the call manager cannot, so an
# import must not either. State restarts at requested on the new portal.
_REQUESTED_OFFERING_EXCLUDED = frozenset({"state", "require_purchase_order"})

# A round's lifecycle after its cut-off is what happened to it on this portal.
_ROUND_EXCLUDED = frozenset(models.Round.LIFECYCLE_FIELDS)

# The most entries an imported document's list may hold, by the list's name:
# well above what a call is configured with, well below what would make one
# import -- and every later read of the call -- slow.
MAX_LIST_ENTRIES = {
    "workflow_steps": 100,
    "notification_rules": 50,
    "notified_proposal_roles": 20,
    "criteria": 50,
    "assigns_roles": 20,
    "input_steps": 100,
    "proposal_roles": 50,
    "panels": 50,
    "notification_overrides": 50,
    "science_domains": 200,
    "science_sub_domains": 500,
    "rounds": 100,
    "requested_offerings": 50,
    "resource_templates": 50,
    "offering_components": 100,
    "pools": 101,
    "checklists": 50,
    "questions": 200,
    "options": 200,
    "role_mappings": 50,
    "documents": 50,
}
# A list the table does not name.
MAX_OTHER_LIST_ENTRIES = 200

_ONE_TO_ONE_CONFIGS = {
    "field_configs": (
        ("proposal_field_config", models.CallProposalFieldConfig),
        ("applicant_visibility_config", models.CallApplicantVisibilityConfig),
    ),
    "review_configs": (
        ("coi_configuration", models.CallCOIConfiguration),
        ("matching_configuration", models.MatchingConfiguration),
        ("assignment_configuration", models.CallAssignmentConfiguration),
    ),
}


def _plain_fields(model, exclude=frozenset()):
    return [
        f
        for f in model._meta.concrete_fields
        if not f.is_relation
        and f.name not in _ALWAYS_EXCLUDED
        and f.name not in exclude
    ]


def _to_portable(value):
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return value.hex
    return value


def _dump(instance, exclude=frozenset()):
    return {
        f.name: _to_portable(f.value_from_object(instance))
        for f in _plain_fields(type(instance), exclude)
    }


def _portable_form_name(checklist, call=None):
    """A form's name in a call document.

    Importing a form the call can already use a form of that name for saves
    the call's copy with the call's name appended (``_Importer._form_name``).
    Exported as it is, that name gains a second call name on the next import,
    and a third on the one after; the document names the form as it was
    defined instead.
    """
    name = checklist.name
    if call is None or checklist.checklist_type != (
        checklist_enums.ChecklistTypes.WORKFLOW_STEP
    ):
        return name
    return form_name_without_call(name, call.name) or name


def form_name_without_call(name, call_name):
    """The name of the form ``name`` copies, when ``name`` is that of the
    copy an import saved for the call ``call_name``; None otherwise."""
    match = re.fullmatch(rf"(.+) \({re.escape(call_name[:60])}(?:, \d+)?\)", name)
    return match.group(1) if match else None


def _checklist_ref(checklist, call=None):
    if not checklist:
        return None
    return {
        "name": _portable_form_name(checklist, call),
        "checklist_type": checklist.checklist_type,
    }


# A select question's answers, conditions and review triggers name its options
# by UUID; the document names them by label.
_SELECT_TYPES = frozenset(
    {
        checklist_enums.QuestionTypes.SINGLE_SELECT,
        checklist_enums.QuestionTypes.MULTI_SELECT,
    }
)
# Question fields holding an answer value, as a dependency's
# required_answer_value does.
_ANSWER_VALUE_FIELDS = ("review_answer_value", "guidance_answer_value")


def _map_options(value, mapping):
    """``value`` -- one option reference or a list of them -- through ``mapping``.

    A reference ``mapping`` does not know raises ``KeyError``.
    """
    if value is None:
        return None
    if isinstance(value, list):
        return [mapping[str(item)] for item in value]
    return mapping[str(value)]


def _option_labels(question, value):
    """``value`` with the options of a select ``question`` named by label."""
    if question.question_type not in _SELECT_TYPES:
        return value
    labels = {}
    for option in question.question_options.all():
        labels[option.uuid.hex] = option.label
        labels[str(uuid.UUID(option.uuid.hex))] = option.label
    try:
        return _map_options(value, labels)
    except KeyError:
        # A stale reference to a deleted option; it matches nothing either way.
        return value


def _dump_checklist(checklist, call=None):
    """The definition of an evaluation form: its questions, their options and
    the dependencies between them. A dependency names the question it depends
    on by its position in ``questions``."""
    questions = list(
        checklist.questions.order_by("order", "id").prefetch_related(
            "question_options", "dependencies__depends_on_question__question_options"
        )
    )
    position = {question.pk: index for index, question in enumerate(questions)}
    entries = []
    for question in questions:
        entry = _dump(question)
        for field in _ANSWER_VALUE_FIELDS:
            entry[field] = _option_labels(question, entry[field])
        entry["options"] = [_dump(option) for option in question.question_options.all()]
        entry["dependencies"] = [
            {
                **_dump(dependency),
                "depends_on": position[dependency.depends_on_question_id],
                "required_answer_value": _option_labels(
                    dependency.depends_on_question, dependency.required_answer_value
                ),
            }
            for dependency in question.dependencies.all()
            if dependency.depends_on_question_id in position
        ]
        entries.append(entry)
    return {
        **_dump(checklist),
        "name": _portable_form_name(checklist, call),
        "questions": entries,
    }


def _role_ref(role):
    if not role:
        return None
    return {"name": role.name}


def _proposal_role_key(role):
    # A call's evaluator role travels with the call; it is named by its key.
    return role.key if role else None


def _portable_contributor_source(call, contributor_source):
    """``contributor_source`` with its evaluator role named by key.

    ``proposal_role:<id or uuid>`` names a role by an identity that does not
    travel with the call; the importing portal resolves the key against the
    roles of the document.
    """
    try:
        kind, parameter = enums.parse_contributor_source(contributor_source)
    except ValueError:
        return contributor_source
    if kind != enums.ContributorSources.PROPOSAL_ROLE:
        return contributor_source
    role = proposal_roles.resolve_role(
        call, f"{proposal_roles.ROLE_REF_PREFIX}{parameter}"
    )
    if role is None:
        return contributor_source
    return f"{proposal_roles.ROLE_REF_PREFIX}{role.key}"


def _dump_rule(rule):
    return {
        **_dump(rule),
        "notified_proposal_roles": [
            _role_ref(role)
            for role in sorted(rule.notified_proposal_roles.all(), key=lambda r: r.name)
        ],
        "proposal_role": _proposal_role_key(rule.proposal_role),
    }


def _export_document(document, warnings):
    if not document.file:
        return None
    # The row can outlive its file. Database storage then reads back empty
    # content rather than failing, so check first; the rest of the call still
    # exports.
    storage, name = document.file.storage, document.file.name
    try:
        if not storage.exists(name):
            raise FileNotFoundError(name)
        with document.file.open("rb") as f:
            content = f.read()
    except OSError:
        warnings.append(
            f"Document '{os.path.basename(name)}' could not be read and was left out."
        )
        return None
    return {
        "description": document.description,
        "filename": os.path.basename(document.file.name),
        "content": base64.b64encode(content).decode("ascii"),
    }


def _call_documents(call):
    # Documents are linked both by FK and through the M2M the API lists; a
    # duplicated call shares the source's rows through the M2M only.
    seen = {}
    for document in list(call.documents.all()) + list(call.calldocument_set.all()):
        seen.setdefault(document.pk, document)
    return list(seen.values())


def export_call(call, sections=None):
    """Return ``(document, warnings)`` for ``call`` and the chosen sections."""
    sections = resolve_sections(sections)
    warnings = []
    data = {
        "schema_version": SCHEMA_VERSION,
        "call": {
            **_dump(call, _CALL_EXCLUDED),
            _CALL_REFERENCE_CODE: call.backend_id,
        },
    }

    # The panels are part of the call's own settings and always travel, by
    # the code and name of each domain they cover: the name keeps a portal
    # with another taxonomy under the same codes from routing them to the
    # wrong domains. Their chairs and members are people
    # of this portal, and stay here.
    data["panels"] = [
        {
            **_dump(panel),
            "science_domains": [
                {"code": d.code, "name": d.name} for d in panel.science_domains.all()
            ],
            "science_sub_domains": [
                {"code": d.code, "name": d.name}
                for d in panel.science_sub_domains.all()
            ],
        }
        for panel in call.panels.prefetch_related(
            "science_domains", "science_sub_domains"
        ).order_by("order", "id")
    ]

    # The call's own wording of its mail travels with its settings too. Left
    # out when it has none, so a call's document reads as before.
    overrides = notification_overrides.dump(call)
    if overrides:
        data["notification_overrides"] = overrides

    if sections["compliance_checklist"]:
        data["compliance_checklist"] = _checklist_ref(call.compliance_checklist)

    if sections["documents"]:
        data["documents"] = [
            doc
            for doc in (
                _export_document(document, warnings)
                for document in _call_documents(call)
            )
            if doc is not None
        ]

    if sections["rounds"]:
        data["rounds"] = [
            _dump(r, _ROUND_EXCLUDED)
            for r in call.round_set.order_by("start_time", "id")
        ]

    if sections["offerings"]:
        # A pool names the round it covers by its place in the exported
        # rounds, or none for the whole call; without the rounds only the
        # call-wide pools travel.
        round_index = {
            pk: index
            for index, pk in enumerate(
                call.round_set.order_by("start_time", "id").values_list("pk", flat=True)
            )
        }

        def _pools(ro, rule):
            dumped = []
            for pool in rule.pools.order_by("created", "id"):
                if pool.round_id is not None and not sections["rounds"]:
                    warnings.append(
                        f"Resource pool of {ro.offering.name} / "
                        f"{rule.component_type} covers a round, which is not "
                        "exported; left out."
                    )
                    continue
                dumped.append(
                    {
                        **_dump(pool),
                        "round": None
                        if pool.round_id is None
                        else round_index[pool.round_id],
                    }
                )
            return dumped

        def _components(ro):
            return [
                {**_dump(rule), "pools": _pools(ro, rule)}
                for rule in ro.component_rules.prefetch_related("pools").order_by(
                    "created", "id"
                )
            ]

        data["requested_offerings"] = [
            {
                **_dump(ro, _REQUESTED_OFFERING_EXCLUDED),
                "offering": {
                    "name": ro.offering.name,
                    "provider_name": ro.offering.customer.name
                    if ro.offering.customer
                    else None,
                },
                "plan": ro.plan.name if ro.plan else None,
                "resource_templates": [_dump(t) for t in ro.ordered_templates],
                "offering_components": _components(ro),
            }
            for ro in call.requestedoffering_set.select_related(
                "offering", "offering__customer", "plan"
            )
            .prefetch_related(
                Prefetch(
                    "callresourcetemplate_set",
                    queryset=models.CallResourceTemplate.objects.order_by("name", "id"),
                    to_attr="ordered_templates",
                )
            )
            .exclude(state=RequestedOfferingStates.CANCELED)
            .order_by("id")
        ]

    if sections["workflow_steps"]:
        # The evaluator roles the steps assign and are completed by.
        data["proposal_roles"] = [
            _dump(role) for role in call.proposal_roles.order_by("created", "id")
        ]
        data["workflow_steps"] = [
            {
                **_dump(step),
                "assigns_roles": [role.key for role in step.assigns_roles.all()],
                "input_steps": [
                    input_step.key
                    for input_step in sorted(
                        step.input_steps.all(), key=lambda s: (s.order, s.id)
                    )
                ],
                "completer_proposal_role": _proposal_role_key(
                    step.completer_proposal_role
                ),
                "contributor_source": _portable_contributor_source(
                    call, step.contributor_source
                ),
                "delegable_to": _portable_contributor_source(call, step.delegable_to),
                "checklist": _checklist_ref(step.checklist, call),
                "notification_rules": [_dump_rule(r) for r in step.ordered_rules],
                "criteria": [_dump(c) for c in step.ordered_criteria],
            }
            for step in call.workflow_steps.select_related(
                "checklist", "completer_proposal_role"
            )
            .prefetch_related(
                "assigns_roles",
                "input_steps",
                Prefetch(
                    "notification_rules",
                    queryset=models.CallWorkflowStepNotificationRule.objects.select_related(
                        "proposal_role"
                    )
                    .order_by("trigger", "recipient", "id")
                    .prefetch_related("notified_proposal_roles"),
                    to_attr="ordered_rules",
                ),
                Prefetch(
                    "criteria",
                    queryset=models.WorkflowCriterion.objects.order_by(
                        "order", "name", "id"
                    ),
                    to_attr="ordered_criteria",
                ),
            )
            .order_by("order", "id")
        ]
        # The evaluation forms those steps use, once each.
        forms = {}
        for step in call.workflow_steps.select_related("checklist").order_by(
            "order", "id"
        ):
            checklist = step.checklist
            if (
                checklist is not None
                and checklist.checklist_type
                == checklist_enums.ChecklistTypes.WORKFLOW_STEP
            ):
                forms.setdefault(checklist.pk, checklist)
        data["checklists"] = [_dump_checklist(c, call) for c in forms.values()]

    for section, configs in _ONE_TO_ONE_CONFIGS.items():
        if not sections[section]:
            continue
        for related_name, _model in configs:
            try:
                data[related_name] = _dump(getattr(call, related_name))
            except ObjectDoesNotExist:
                data[related_name] = None

    if sections["role_mappings"]:
        data["role_mappings"] = [
            {
                "proposal_role": _role_ref(m.proposal_role),
                "project_role": _role_ref(m.project_role),
            }
            for m in call.proposalprojectrolemapping_set.select_related(
                "proposal_role", "project_role"
            ).order_by("id")
        ]

    return data, warnings


def resolve_sections(overrides=None):
    sections = dict.fromkeys(SECTIONS, True)
    if overrides:
        sections.update({k: bool(v) for k, v in overrides.items() if k in sections})
    return sections


class _Importer:
    def __init__(self, data, manager, user, sections, name=None):
        self.data = data
        self.manager = manager
        self.user = user
        self.sections = resolve_sections(sections)
        self.name = name
        self.warnings = []
        # Whether a panel named a domain by its code alone, warned once.
        self.matched_by_code_only = False
        self.imported = []
        self.proposal_roles = {}
        # The evaluation forms the document defines, by (name, type), and the
        # copies made of them for this call.
        self.form_definitions = {}
        self.forms = {}
        # The rounds imported, in the document's order: budgets name them by
        # their place in it.
        self.rounds = []

    def warn(self, message):
        self.warnings.append(message)

    def mark_imported(self, section, count):
        if count:
            self.imported.append(section)

    # -- shape checks -------------------------------------------------------

    @staticmethod
    def _mapping(value, context):
        if not isinstance(value, dict):
            raise serializers.ValidationError({context: "Expected a mapping."})
        return value

    @staticmethod
    def _list(value, context):
        if value is None:
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError({context: "Expected a list."})
        name = re.sub(r"\[\d+\]$", "", context.rsplit(".", 1)[-1])
        maximum = MAX_LIST_ENTRIES.get(name, MAX_OTHER_LIST_ENTRIES)
        if len(value) > maximum:
            raise serializers.ValidationError(
                {
                    context: (
                        f"At most {maximum} entries are accepted here; the "
                        f"document has {len(value)}."
                    )
                }
            )
        return value

    @staticmethod
    def _text(value, context, required=False):
        if value is None or value == "":
            if required:
                raise serializers.ValidationError({context: "This field is required."})
            return None
        if not isinstance(value, str):
            raise serializers.ValidationError({context: "Expected a string."})
        return value

    # -- generic field handling -------------------------------------------

    def _assign(self, instance, values, context, exclude=frozenset()):
        """Set plain fields on ``instance`` from ``values``.

        Keys this portal does not know (an export from a newer version) are
        skipped with a warning rather than failing the whole import.
        """
        self._mapping(values, context)
        fields = {f.name: f for f in _plain_fields(type(instance), exclude)}
        for key, value in values.items():
            field = fields.get(key)
            if field is None:
                if key not in _NESTED_KEYS:
                    self.warn(f"{context}: field '{key}' is not supported, ignored.")
                continue
            try:
                setattr(instance, field.attname, field.to_python(value))
            except DjangoValidationError as e:
                raise serializers.ValidationError({f"{context}.{key}": e.messages})
        return instance

    def _validate(self, instance, context, run_clean=True, exclude=()):
        """Run the model's own validation, as a ModelForm would.

        Every plain field is checked -- including ones the document left out,
        so a missing required value is a 400 and not a crash in save() -- as
        are uniqueness constraints and, unless deferred, ``clean()``.
        Relations are set by the importer itself and are not re-checked, and
        identity fields are filled in by save().
        """
        identity = _ALWAYS_EXCLUDED.union(exclude)
        relations = {f.name for f in instance._meta.concrete_fields if f.is_relation}
        try:
            instance.clean_fields(exclude=identity | relations)
            if run_clean:
                instance.clean()
            # Relations stay in: Django skips a unique_together group, such as
            # (call, name), as soon as any field of it is excluded.
            instance.validate_unique(exclude=identity)
            instance.validate_constraints(exclude=identity)
        except DjangoValidationError as e:
            raise serializers.ValidationError({context: e.messages})
        except serializers.ValidationError as e:
            # Some proposal models raise DRF's ValidationError from clean().
            raise serializers.ValidationError({context: e.detail})

    # -- reference resolution ---------------------------------------------

    def _unique(self, queryset, what):
        matches = list(queryset[:2])
        if not matches:
            self.warn(f"{what} was not found on this portal, skipped.")
            return None
        if len(matches) > 1:
            self.warn(f"{what} matches more than one object on this portal, skipped.")
            return None
        return matches[0]

    def _checklist(self, ref, what, context):
        if ref is None:
            return None
        self._mapping(ref, context)
        name = self._text(ref.get("name"), f"{context}.name", required=True)
        checklist_type = self._text(
            ref.get("checklist_type"), f"{context}.checklist_type", required=True
        )
        return self._unique(
            self._usable_forms().filter(name=name, checklist_type=checklist_type),
            f"{what} checklist '{name}'",
        )

    def _usable_forms(self):
        """The forms the imported call may use: those the whole portal
        shares and those of the organisation running the call. Another
        organisation's forms are neither matched nor revealed."""
        return checklist_models.Checklist.objects.filter(
            Q(organization__isnull=True) | Q(organization_id=self.manager.customer_id)
        )

    def _role(self, ref, model, what, context):
        self._mapping(ref, context)
        name = self._text(ref.get("name"), f"{context}.name", required=True)
        return self._unique(
            permissions_models.Role.objects.filter(
                name=name,
                is_active=True,
                content_type=ContentType.objects.get_for_model(model),
            ),
            f"{what} role '{name}'",
        )

    def _offering(self, ref, context):
        self._mapping(ref, context)
        name = self._text(ref.get("name"), f"{context}.name", required=True)
        provider = self._text(
            ref.get("provider_name"), f"{context}.provider_name", required=True
        )
        queryset = (
            marketplace_models.Offering.objects.filter(
                name=name, customer__name=provider
            )
            .exclude(state=marketplace_models.Offering.States.ARCHIVED)
            .filter_by_ordering_availability_for_user(self.user)
        )
        return self._unique(queryset, f"Offering '{name}' of provider '{provider}'")

    # -- sections -----------------------------------------------------------

    def _section(self, section, *keys):
        """Whether ``section`` is requested and the document carries it."""
        return self.sections[section] and any(k in self.data for k in keys)

    def run(self):
        version = self.data.get("schema_version")
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise serializers.ValidationError(
                {"schema_version": f"Unsupported schema version: {version!r}."}
            )

        call = self._import_call()
        if self.data.get("tracks"):
            # A call has no application tracks: a document that carries
            # them imports without.
            self.warn("tracks: application tracks are not supported, ignored.")
        if "panels" in self.data:
            self._import_panels(call)
        if "notification_overrides" in self.data:
            self._import_notification_overrides(call)
        if self._section("rounds", "rounds"):
            self._import_rounds(call)
        if self._section("offerings", "requested_offerings"):
            self._import_offerings(call)
        if self._section("workflow_steps", "workflow_steps", "proposal_roles"):
            self._import_proposal_roles(call)
            self._import_workflow_steps(call)
        for section, configs in _ONE_TO_ONE_CONFIGS.items():
            if self._section(section, *(name for name, _ in configs)):
                self._import_one_to_one(call, section, configs)
        if self._section("role_mappings", "role_mappings"):
            self._import_role_mappings(call)
        if self._section("documents", "documents"):
            self._import_documents(call)
        # Checked once the allocation decision is in: rounds come first.
        if call.has_open_ended_round() and (
            conflict := call.open_ended_round_conflict()
        ):
            raise serializers.ValidationError({"rounds": conflict})
        return call

    def _import_call(self):
        call = models.Call(
            manager=self.manager, created_by=self.user, state=CallStates.DRAFT
        )
        values = self._mapping(self.data.get("call"), "call")
        reference_code = self._text(
            values.get(_CALL_REFERENCE_CODE), f"call.{_CALL_REFERENCE_CODE}"
        )
        self._assign(call, values, "call", _CALL_EXCLUDED)
        if reference_code:
            max_length = models.Call._meta.get_field("backend_id").max_length
            if len(reference_code) > max_length:
                raise serializers.ValidationError(
                    {
                        f"call.{_CALL_REFERENCE_CODE}": (
                            f"Ensure this field has no more than {max_length} "
                            "characters."
                        )
                    }
                )
            call.backend_id = reference_code
        if self.name:
            call.name = self.name

        if call.order_author == OrderAuthors.SPECIFIC_USER:
            # The named contact is a person on the source portal.
            call.order_author = OrderAuthors.CALL_MANAGER
            self.warn(
                "Orders were attributed to a named contact; they are attributed "
                "to the call manager instead. Choose the contact on this portal."
            )

        if self._section("compliance_checklist", "compliance_checklist"):
            call.compliance_checklist = self._checklist(
                self.data["compliance_checklist"],
                "Compliance",
                "compliance_checklist",
            )
            self.mark_imported(
                "compliance_checklist", call.compliance_checklist is not None
            )

        self._validate(call, "call")
        call.save()
        return call

    def _import_notification_overrides(self, call):
        """The call's own wording of its mail. One the portal cannot word --
        an unknown mail type, a template the sandbox refuses -- is left out
        with a warning, and the portal's wording is sent instead."""
        seen = set()
        entries = self._list(
            self.data["notification_overrides"], "notification_overrides"
        )
        for index, values in enumerate(entries):
            context = f"notification_overrides[{index}]"
            self._mapping(values, context)
            mail_type = self._text(
                values.get("mail_type"), f"{context}.mail_type", required=True
            )
            variant = self._text(values.get("variant"), f"{context}.variant") or ""
            subject = self._text(values.get("subject"), f"{context}.subject") or ""
            body_text = (
                self._text(values.get("body_text"), f"{context}.body_text") or ""
            )
            body_html = (
                self._text(values.get("body_html"), f"{context}.body_html") or ""
            )
            definition = notification_overrides.get_type(mail_type, variant)
            key = notification_overrides.mail_key(mail_type, variant)
            if definition is None:
                self.warn(
                    f"The wording of mail '{key}' is not a mail of a call; left out."
                )
                continue
            if key in seen:
                self.warn(
                    f"The wording of mail '{key}' is given twice; the first is kept."
                )
                continue
            try:
                notification_overrides.validate(
                    definition, subject, body_text, body_html
                )
            except notification_overrides.OverrideError as error:
                self.warn(
                    f"The wording of mail '{key}' cannot be rendered "
                    f"({error.errors}); left out."
                )
                continue
            seen.add(key)
            models.CallNotificationOverride.objects.create(
                call=call,
                mail_type=mail_type,
                variant=variant,
                subject=subject,
                body_text=body_text,
                body_html=body_html,
                enabled=values.get("enabled") is not False,
                updated_by=self.user if getattr(self.user, "pk", None) else None,
            )

    def _import_panels(self, call):
        covered = set()
        for index, values in enumerate(self._list(self.data["panels"], "panels")):
            context = f"panels[{index}]"
            panel = self._assign(models.CallPanel(call=call), values, context)
            if not panel.key:
                raise serializers.ValidationError(
                    {f"{context}.key": "This field is required."}
                )
            self._validate(panel, context)
            panel.save()
            for field, model, what in (
                ("science_domains", structure_models.ScienceDomain, "Science domain"),
                (
                    "science_sub_domains",
                    structure_models.ScienceSubDomain,
                    "Science sub-domain",
                ),
            ):
                found = []
                for entry in self._list(values.get(field), f"{context}.{field}"):
                    target = self._science_domain(
                        model, what, entry, f"{context}.{field}"
                    )
                    if target is None:
                        continue
                    if (field, target.pk) in covered:
                        raise serializers.ValidationError(
                            {
                                f"{context}.{field}": (
                                    f"{what} '{target.code}' already goes to "
                                    "another panel."
                                )
                            }
                        )
                    covered.add((field, target.pk))
                    found.append(target)
                getattr(panel, field).set(found)

    def _science_domain(self, model, what, entry, context):
        """The domain or sub-domain a panel's ``entry`` names on this portal.

        An entry is ``{"code", "name"}``, matched by both: a portal whose
        taxonomy gives the code to another domain gets a warning naming the
        two, not a panel routing the wrong proposals. A bare code, as older
        exports wrote it, is matched by code alone.
        """
        if isinstance(entry, dict):
            code = self._text(entry.get("code"), f"{context}.code", required=True)
            name = self._text(entry.get("name"), f"{context}.name")
        else:
            code = self._text(entry, context, required=True)
            name = None
        target = self._unique(model.objects.filter(code=code), f"{what} '{code}'")
        if target is None:
            return None
        if name is None:
            if not self.matched_by_code_only:
                self.matched_by_code_only = True
                self.warn(
                    "The panels give their science domains by code only, as "
                    "older exports did: they were matched by code alone."
                )
            return target
        if target.name.strip().casefold() != name.strip().casefold():
            self.warn(
                f"{what} '{code}' is '{target.name}' here, the imported call "
                f"expects '{name}', skipped."
            )
            return None
        return target

    def _import_documents(self, call):
        count = 0
        for index, doc in enumerate(self._list(self.data["documents"], "documents")):
            context = f"documents[{index}]"
            self._mapping(doc, context)
            try:
                content = base64.b64decode(
                    self._text(doc.get("content"), f"{context}.content") or "",
                    validate=True,
                )
            except ValueError:
                raise serializers.ValidationError(
                    {context: "Document content is not valid base64."}
                )
            filename = self._text(doc.get("filename"), f"{context}.filename")
            document = models.CallDocument(
                call=call,
                description=self._text(doc.get("description"), context) or "",
            )
            # The file is written below, once the row is known to be valid.
            self._validate(document, context, exclude={"file"})
            document.file.save(
                os.path.basename(filename or "") or "document",
                ContentFile(content),
                save=False,
            )
            document.save()
            call.documents.add(document)
            count += 1
        self.mark_imported("documents", count)

    def _import_rounds(self, call):
        created = []
        for index, values in enumerate(self._list(self.data["rounds"], "rounds")):
            context = f"rounds[{index}]"
            round_obj = self._assign(
                models.Round(call=call), values, context, _ROUND_EXCLUDED
            )
            self._validate(round_obj, context)
            # The rules ProtectedRoundSerializer applies to a round created
            # through the API.
            start, cutoff = round_obj.start_time, round_obj.cutoff_time
            if cutoff is not None and cutoff <= start:
                raise serializers.ValidationError(
                    {context: "Cutoff time must be later than start time."}
                )
            # A missing cut-off is +infinity on either side.
            if any(
                (cutoff is None or other.start_time < cutoff)
                and (other.cutoff_time is None or other.cutoff_time > start)
                for other in created
            ):
                raise serializers.ValidationError(
                    {context: "Round is overlapping with another round."}
                )
            round_obj.save()
            created.append(round_obj)
        self.rounds = created
        self.mark_imported("rounds", len(created))

    def _import_offerings(self, call):
        count = 0
        entries = self._list(self.data["requested_offerings"], "requested_offerings")
        for index, values in enumerate(entries):
            context = f"requested_offerings[{index}]"
            self._mapping(values, context)
            offering = self._offering(values.get("offering"), f"{context}.offering")
            if not offering:
                continue
            requested = models.RequestedOffering(
                call=call,
                offering=offering,
                created_by=self.user,
                state=RequestedOfferingStates.REQUESTED,
            )
            plan_name = self._text(values.get("plan"), f"{context}.plan")
            if plan_name:
                requested.plan = self._unique(
                    offering.plans.filter(name=plan_name, archived=False),
                    f"Plan '{plan_name}' of offering '{offering.name}'",
                )
            self._assign(requested, values, context, _REQUESTED_OFFERING_EXCLUDED)
            self._validate(requested, context)
            requested.save()
            templates = self._list(
                values.get("resource_templates"), f"{context}.resource_templates"
            )
            for t_index, template_values in enumerate(templates):
                t_context = f"{context}.resource_templates[{t_index}]"
                template = self._assign(
                    models.CallResourceTemplate(
                        call=call, requested_offering=requested, created_by=self.user
                    ),
                    template_values,
                    t_context,
                )
                self._validate(template, t_context)
                template.save()
            self._import_components(requested, values, context)
            count += 1
        self.mark_imported("offerings", count)

    def _import_components(self, requested, values, context):
        """The offering's component rules and the pools on them, held to the
        rules the API holds them to: a rule's own fields and a component the
        offering's plan prices on this portal, bounds the offering's resource
        templates fit in, and one pool per rule and round. A rule or pool
        breaking one is left out with a warning; the rest of the call still
        imports.

        The offering comes in requested, as every imported one does, with
        its rules: they bound what a proposal requests once its provider
        accepts the request."""
        entries = self._list(
            values.get("offering_components"), f"{context}.offering_components"
        )
        for c_index, rule_values in enumerate(entries):
            c_context = f"{context}.offering_components[{c_index}]"
            self._mapping(rule_values, c_context)
            rule = self._assign(
                models.CallOfferingComponent(
                    requested_offering=requested, created_by=self.user
                ),
                rule_values,
                c_context,
            )
            try:
                self._validate(rule, c_context)
            except serializers.ValidationError as exc:
                # The offering on this portal may not price the component,
                # or bound it differently: the rest of the call still imports.
                self.warn(f"{c_context}: {exc.detail}; component rule left out.")
                continue
            conflicts = budgets.rule_template_conflicts(rule)
            if conflicts:
                self.warn(
                    f"{c_context}: a resource template of the offering is "
                    f"outside its bounds ({'; '.join(conflicts)}). Component "
                    "rule left out."
                )
                continue
            rule.save()
            self._import_pools(rule, rule_values, c_context)

    def _import_pools(self, rule, values, context):
        seen = set()
        for p_index, pool_values in enumerate(
            self._list(values.get("pools"), f"{context}.pools")
        ):
            p_context = f"{context}.pools[{p_index}]"
            self._mapping(pool_values, p_context)
            index = pool_values.get("round")
            if index is not None and (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < len(self.rounds)
            ):
                self.warn(
                    f"{p_context}: the round it covers was not imported; pool left out."
                )
                continue
            call_round = None if index is None else self.rounds[index]
            if index in seen:
                self.warn(
                    f"{p_context}: the component already has a pool for "
                    f"{'the whole call' if index is None else 'this round'}; "
                    "pool left out."
                )
                continue
            pool = self._assign(
                models.CallResourcePool(
                    offering_component=rule, round=call_round, created_by=self.user
                ),
                pool_values,
                p_context,
            )
            try:
                self._validate(pool, p_context)
            except serializers.ValidationError as exc:
                self.warn(f"{p_context}: {exc.detail}; pool left out.")
                continue
            pool.save()
            seen.add(index)

    @staticmethod
    def _upgrade_step(values):
        """Read a step written before steps had a kind.

        Such a step names one of the fixed catalogue steps in ``step`` and its
        actor in ``responsible_role``; its kind, name and place come from the
        default step of that key, as the migration gave every existing call.
        """
        values = dict(values)
        legacy_key = values.pop("step", None)
        legacy_role = values.pop("responsible_role", None)
        if "key" not in values and legacy_key is not None:
            values["key"] = legacy_key
        if "completer" not in values and legacy_role is not None:
            values["completer"] = legacy_role
        # min_reviewers was renamed when steps began to count contributions.
        legacy_min = values.pop("min_reviewers", None)
        if "min_contributions" not in values and legacy_min is not None:
            values["min_contributions"] = legacy_min
        default = DEFAULT_CALL_STEPS_MAP.get(values.get("key"))
        if default is not None and "kind" not in values:
            values.update(
                kind=default.kind,
                name=default.name,
                description=default.description,
                outcome_set=default.outcome_set,
                on_negative=default.on_negative,
                is_allocation_decision=default.is_allocation_decision,
                contributor_source=default.contributor_source,
            )
            values["completer"] = values.get("completer") or default.completer
            values.setdefault("order", DEFAULT_CALL_STEPS.index(default))
        return values

    def _import_proposal_roles(self, call):
        for index, values in enumerate(
            self._list(self.data.get("proposal_roles"), "proposal_roles")
        ):
            context = f"proposal_roles[{index}]"
            role = self._assign(models.CallProposalRole(call=call), values, context)
            if not role.key:
                raise serializers.ValidationError(
                    {f"{context}.key": "This field is required."}
                )
            self._validate(role, context)
            role.save()
            self.proposal_roles[role.key] = role

    def _proposal_role(self, key, context):
        key = self._text(key, context)
        if key is None:
            return None
        role = self.proposal_roles.get(key)
        if role is None:
            raise serializers.ValidationError(
                {context: f"The document defines no evaluator role '{key}'."}
            )
        return role

    def _contributor_source(self, contributor_source, context):
        """``contributor_source`` with the evaluator role it names by key
        pointed at that role of the imported call.

        A role the document does not define -- or one named by the id or
        uuid it had on the exporting portal -- is refused: the step would
        resolve to nobody.
        """
        try:
            kind, parameter = enums.parse_contributor_source(contributor_source)
        except ValueError:
            # clean() reports it.
            return contributor_source
        if kind != enums.ContributorSources.PROPOSAL_ROLE:
            return contributor_source
        role = self.proposal_roles.get(parameter)
        if role is None:
            raise serializers.ValidationError(
                {
                    context: (
                        f"The document defines no evaluator role '{parameter}'; "
                        "name the role by its key: proposal_role:<key>."
                    )
                }
            )
        return f"{proposal_roles.ROLE_REF_PREFIX}{role.pk}"

    # -- evaluation forms -----------------------------------------------------

    def _read_form_definitions(self):
        """Index the forms the document defines by (name, type)."""
        for index, values in enumerate(
            self._list(self.data.get("checklists"), "checklists")
        ):
            context = f"checklists[{index}]"
            self._mapping(values, context)
            name = self._text(values.get("name"), f"{context}.name", required=True)
            checklist_type = self._text(
                values.get("checklist_type"),
                f"{context}.checklist_type",
                required=True,
            )
            if checklist_type != checklist_enums.ChecklistTypes.WORKFLOW_STEP:
                # Other checklists are the portal's own, not a call's.
                raise serializers.ValidationError(
                    {
                        f"{context}.checklist_type": (
                            "Only workflow step forms can be defined in the document."
                        )
                    }
                )
            if (name, checklist_type) in self.form_definitions:
                raise serializers.ValidationError(
                    {f"{context}.name": f"Form '{name}' is defined twice."}
                )
            self.form_definitions[(name, checklist_type)] = (values, context)

    def _step_form(self, call, ref, context):
        """The form a step uses: the call's own copy of a form the document
        defines, else the portal's form of that name."""
        if ref is None:
            return None
        self._mapping(ref, context)
        key = (
            self._text(ref.get("name"), f"{context}.name", required=True),
            self._text(
                ref.get("checklist_type"), f"{context}.checklist_type", required=True
            ),
        )
        if key in self.forms:
            return self.forms[key]
        if key not in self.form_definitions:
            return self._checklist(ref, "Workflow step", context)
        values, definition_context = self.form_definitions[key]
        self.forms[key] = self._create_form(call, values, definition_context)
        return self.forms[key]

    def _form_name(self, name, call):
        """``name``, or -- when the call can already use a checklist of that
        name -- ``name`` followed by the call's name, so that binding a form
        by name stays unambiguous. Other organisations' forms do not count:
        they are out of the call's reach, and the name must not tell of
        them."""
        max_length = checklist_models.Checklist._meta.get_field("name").max_length
        candidates = itertools.chain(
            [name],
            (
                f" ({call.name[:60]}{'' if n == 1 else f', {n}'})"
                for n in itertools.count(1)
            ),
        )
        for index, candidate in enumerate(candidates):
            if index:
                candidate = name[: max_length - len(candidate)] + candidate
            if not self._usable_forms().filter(name=candidate).exists():
                return candidate

    def _create_form(self, call, values, context):
        """The call's own copy of a form the document defines.

        This is the one way someone who is not staff creates a form -- and
        its questions -- that the checklist administration would otherwise
        create: whoever may create calls for the organisation. The form
        belongs to that organisation alone; its questions and their
        dependencies are held to the rules the checklist administration
        applies, the upload settings included; and the document's lists are
        bounded (``MAX_LIST_ENTRIES``)."""
        checklist = self._assign(
            checklist_models.Checklist(organization_id=call.manager.customer_id),
            values,
            context,
        )
        checklist.name = self._form_name(checklist.name, call)
        self._validate(checklist, context)
        checklist.save()

        questions = []
        for index, question_values in enumerate(
            self._list(values.get("questions"), f"{context}.questions")
        ):
            question_context = f"{context}.questions[{index}]"
            questions.append(
                self._create_question(
                    checklist, index, question_values, question_context
                )
            )
        # A dependency names an earlier or later question by its position.
        for question, question_values, question_context in questions:
            for index, dependency_values in enumerate(
                self._list(
                    question_values.get("dependencies"),
                    f"{question_context}.dependencies",
                )
            ):
                self._create_dependency(
                    question,
                    [q for q, _, _ in questions],
                    dependency_values,
                    f"{question_context}.dependencies[{index}]",
                )
        return checklist

    def _option_uuids(self, question, value, context):
        """``value`` with the options of a select ``question`` named by UUID."""
        if question.question_type not in _SELECT_TYPES or value is None:
            return value
        uuids = {o.label: o.uuid.hex for o in question.question_options.all()}
        try:
            return _map_options(value, uuids)
        except KeyError as e:
            raise serializers.ValidationError(
                {context: f"The question has no option {e.args[0]!r}."}
            )

    def _create_question(self, checklist, index, values, context):
        self._mapping(values, context)
        question = self._assign(
            checklist_models.Question(checklist=checklist), values, context
        )
        if "order" not in values:
            question.order = index
        self._validate(question, context)
        question.save()

        labels = set()
        for option_index, option_values in enumerate(
            self._list(values.get("options"), f"{context}.options")
        ):
            option_context = f"{context}.options[{option_index}]"
            option = self._assign(
                checklist_models.QuestionOption(question=question),
                option_values,
                option_context,
            )
            if option.label in labels:
                raise serializers.ValidationError(
                    {option_context: f"Option '{option.label}' is listed twice."}
                )
            labels.add(option.label)
            if "order" not in option_values:
                option.order = option_index
            self._validate(option, option_context)
            option.save()

        for field in _ANSWER_VALUE_FIELDS:
            setattr(
                question,
                field,
                self._option_uuids(
                    question, getattr(question, field), f"{context}.{field}"
                ),
            )
        question.save()
        # The rules the checklist admin API applies to a question, given the
        # fields the question sets.
        attrs = {
            f.name: getattr(question, f.attname)
            for f in _plain_fields(checklist_models.Question)
            if getattr(question, f.attname) != f.get_default()
        }
        attrs["question_type"] = question.question_type
        if not question.review_answer_value:
            # A review operator with no value to compare against triggers
            # nothing; forms made outside the admin API carry such leftovers.
            attrs.pop("operator", None)
        try:
            checklist_serializers.QuestionAdminSerializer().validate(attrs)
        except serializers.ValidationError as e:
            raise serializers.ValidationError({context: e.detail})
        return question, values, context

    def _create_dependency(self, question, questions, values, context):
        self._mapping(values, context)
        position = values.get("depends_on")
        if (
            not isinstance(position, int)
            or isinstance(position, bool)
            or not 0 <= position < len(questions)
        ):
            raise serializers.ValidationError(
                {
                    f"{context}.depends_on": (
                        "Expected the position of a question of this form."
                    )
                }
            )
        depends_on = questions[position]
        operator = self._text(values.get("operator"), f"{context}.operator") or (
            "equals"
        )
        required_answer_value = self._option_uuids(
            depends_on,
            values.get("required_answer_value"),
            f"{context}.required_answer_value",
        )
        try:
            checklist_serializers.QuestionDependencySerializer().validate(
                {
                    "question": question,
                    "depends_on_question": depends_on,
                    "required_answer_value": required_answer_value,
                    "operator": operator,
                }
            )
        except serializers.ValidationError as e:
            raise serializers.ValidationError({context: e.detail})
        dependency = checklist_models.QuestionDependency(
            question=question,
            depends_on_question=depends_on,
            required_answer_value=required_answer_value,
            operator=operator,
        )
        self._validate(dependency, context)
        dependency.save()

    def _import_workflow_steps(self, call):
        entries = []
        for index, values in enumerate(
            self._list(self.data.get("workflow_steps"), "workflow_steps")
        ):
            context = f"workflow_steps[{index}]"
            values = self._upgrade_step(self._mapping(values, context))
            key = self._text(values.get("key"), f"{context}.key", required=True)
            entries.append((key, values, context))
        if not entries:
            return
        self._read_form_definitions()
        keys = [key for key, _, _ in entries]
        duplicates = sorted(key for key, count in Counter(keys).items() if count > 1)
        if duplicates:
            raise serializers.ValidationError(
                {"workflow_steps": f"Duplicate step keys: {', '.join(duplicates)}."}
            )

        # Creating the call seeded the default steps and their notification
        # rules; the export replaces both. Seeded steps it leaves out go, and
        # the rest step out of the way of the orders the export gives.
        call.workflow_steps.exclude(key__in=keys).delete()
        existing = {step.key: step for step in call.workflow_steps.all()}
        utils.park_steps_out_of_the_way(
            existing.values(), [values.get("order") for _, values, _ in entries]
        )

        steps = []
        for key, values, context in entries:
            step = existing.get(key) or models.CallWorkflowStep(call=call)
            # Parked above; an entry that names no order is appended.
            step.order = None
            self._assign(step, values, context)
            step.checklist = self._step_form(
                call, values.get("checklist"), f"{context}.checklist"
            )
            assigns_roles = [
                role
                for role_index, key in enumerate(
                    self._list(values.get("assigns_roles"), f"{context}.assigns_roles")
                )
                if (
                    role := self._proposal_role(
                        key, f"{context}.assigns_roles[{role_index}]"
                    )
                )
            ]
            try:
                models.CallWorkflowStep.check_assigned_roles(
                    step.kind, call.pk, assigns_roles
                )
            except DjangoValidationError as exc:
                raise serializers.ValidationError(
                    {f"{context}.assigns_roles": exc.message_dict["assigns_roles"]}
                )
            step.completer_proposal_role = self._proposal_role(
                values.get("completer_proposal_role"),
                f"{context}.completer_proposal_role",
            )
            step.contributor_source = self._contributor_source(
                step.contributor_source, f"{context}.contributor_source"
            )
            # Names an evaluator role the way a contributor source does.
            step.delegable_to = self._contributor_source(
                step.delegable_to, f"{context}.delegable_to"
            )
            step.apply_kind_defaults()
            # clean() checks the step against the other enabled steps, so it
            # runs once all of them are in place.
            self._validate(step, context, run_clean=False)
            step.save()
            step.assigns_roles.set(assigns_roles)

            step.notification_rules.all().delete()
            rules = self._list(
                values.get("notification_rules"), f"{context}.notification_rules"
            )
            for r_index, rule_values in enumerate(rules):
                r_context = f"{context}.notification_rules[{r_index}]"
                rule = self._assign(
                    models.CallWorkflowStepNotificationRule(workflow_step=step),
                    rule_values,
                    r_context,
                )
                rule.proposal_role = self._proposal_role(
                    rule_values.get("proposal_role"), f"{r_context}.proposal_role"
                )
                self._validate(rule, r_context)
                rule.save()
                roles = []
                for role_index, ref in enumerate(
                    self._list(
                        rule_values.get("notified_proposal_roles"),
                        f"{r_context}.notified_proposal_roles",
                    )
                ):
                    role = self._role(
                        ref,
                        models.Proposal,
                        "Proposal",
                        f"{r_context}.notified_proposal_roles[{role_index}]",
                    )
                    if role:
                        roles.append(role)
                rule.notified_proposal_roles.set(roles)

            step.criteria.all().delete()
            criteria = self._list(values.get("criteria"), f"{context}.criteria")
            for c_index, criterion_values in enumerate(criteria):
                c_context = f"{context}.criteria[{c_index}]"
                criterion = self._assign(
                    models.WorkflowCriterion(workflow_step=step),
                    criterion_values,
                    c_context,
                )
                self._validate(criterion, c_context)
                criterion.save()
            steps.append((step, context))

        # Input steps name other steps of the document, so they are set once
        # every step exists.
        by_key = {step.key: step for step, _ in steps}
        for (step, context), (_, values, _) in zip(steps, entries):
            inputs = []
            for input_index, input_key in enumerate(
                self._list(values.get("input_steps"), f"{context}.input_steps")
            ):
                input_context = f"{context}.input_steps[{input_index}]"
                input_key = self._text(input_key, input_context)
                if input_key not in by_key:
                    raise serializers.ValidationError(
                        {input_context: f"The document defines no step '{input_key}'."}
                    )
                inputs.append(by_key[input_key])
            try:
                models.CallWorkflowStep.check_input_steps(
                    step.pk, call.pk, step.order, inputs, step.phase
                )
            except DjangoValidationError as exc:
                raise serializers.ValidationError(
                    {f"{context}.input_steps": exc.message_dict["input_steps"]}
                )
            step.input_steps.set(inputs)
        try:
            models.CallWorkflowStep.check_responses_decided(call.pk)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                {"workflow_steps": exc.message_dict["input_steps"]}
            )
        try:
            models.CallWorkflowStep.check_roles_assigned_first(call.pk)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                {"workflow_steps": exc.message_dict["order"]}
            )
        try:
            models.CallWorkflowStep.check_stages_and_conditions(call.pk)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                {
                    "workflow_steps": [
                        message
                        for messages in exc.message_dict.values()
                        for message in messages
                    ]
                }
            )

        for step, context in steps:
            self._validate(step, context)
        self.mark_imported("workflow_steps", len(steps))
        for name, _ in sorted(self.form_definitions.keys() - self.forms.keys()):
            self.warn(f"Form '{name}' is used by no workflow step, skipped.")

    def _import_one_to_one(self, call, section, configs):
        count = 0
        for related_name, model in configs:
            values = self.data.get(related_name)
            if values is None:
                continue
            # Some of these are seeded when the call is created.
            instance = model.objects.filter(call=call).first() or model(call=call)
            self._assign(instance, values, related_name)
            self._validate(instance, related_name)
            instance.save()
            count += 1
        self.mark_imported(section, count)

    def _import_role_mappings(self, call):
        # The document's mappings replace the defaults the call was seeded with.
        call.proposalprojectrolemapping_set.all().delete()
        count = 0
        for index, values in enumerate(
            self._list(self.data["role_mappings"], "role_mappings")
        ):
            context = f"role_mappings[{index}]"
            self._mapping(values, context)
            proposal_role = self._role(
                values.get("proposal_role"),
                models.Proposal,
                "Proposal",
                f"{context}.proposal_role",
            )
            if not proposal_role:
                continue
            project_role = None
            if values.get("project_role") is not None:
                project_role = self._role(
                    values["project_role"],
                    structure_models.Project,
                    "Project",
                    f"{context}.project_role",
                )
                if not project_role:
                    continue
            mapping = models.ProposalProjectRoleMapping(
                call=call, proposal_role=proposal_role, project_role=project_role
            )
            self._validate(mapping, context)
            mapping.save()
            count += 1
        self.mark_imported("role_mappings", count)


# Keys nested inside an exported object that are not model fields.
_NESTED_KEYS = frozenset(
    {
        "science_domains",
        "science_sub_domains",
        "offering",
        "plan",
        "resource_templates",
        "offering_components",
        "pools",
        "round",
        "rounds",
        "checklist",
        "notification_rules",
        "notified_proposal_roles",
        "proposal_role",
        "assigns_roles",
        "input_steps",
        "completer_proposal_role",
        "criteria",
        "questions",
        "options",
        "dependencies",
        _CALL_REFERENCE_CODE,
    }
)


@transaction.atomic
def import_call(data, manager, user, sections=None, name=None):
    """Create a draft call under ``manager`` from an exported document.

    Returns ``(call, imported_sections, warnings)``. Any validation error rolls
    back everything created so far. That includes document files: the media
    storage keeps them as database rows, inside the same transaction.
    """
    if not isinstance(data, dict):
        raise serializers.ValidationError({"call_data": "Expected a mapping."})
    importer = _Importer(data, manager, user, sections, name=name)
    call = importer.run()
    return call, importer.imported, importer.warnings
