"""Export a call's configuration to a portable document and import it elsewhere.

The document is a template, not a data dump: it carries the call's settings and
the configuration hanging off it, never proposals, reviews, reviewer pools,
assignments or anything naming a user. Nothing in it is identified by UUID --
offerings, plans, checklists and roles are referenced by name, because the
portal importing it has its own identifiers for all of them.

Plain (non-relational) model fields are copied generically, so a field added to
one of these models later travels without touching this module. Relations are
translated explicitly, and anything not listed here is left out.
"""

import base64
import datetime
import decimal
import os
import uuid

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Prefetch
from rest_framework import serializers

from waldur_core.checklist import models as checklist_models
from waldur_core.permissions import models as permissions_models
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    CallStates,
    OrderAuthors,
    RequestedOfferingStates,
)

SCHEMA_VERSION = 1

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
_CALL_EXCLUDED = frozenset({"state"})
# The provider sets require_purchase_order; the call manager cannot, so an
# import must not either. State restarts at requested on the new portal.
_REQUESTED_OFFERING_EXCLUDED = frozenset({"state", "require_purchase_order"})

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


def _checklist_ref(checklist):
    if not checklist:
        return None
    return {"name": checklist.name, "checklist_type": checklist.checklist_type}


def _role_ref(role):
    if not role:
        return None
    return {"name": role.name}


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
        "call": _dump(call, _CALL_EXCLUDED),
    }

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
        data["rounds"] = [_dump(r) for r in call.round_set.order_by("start_time", "id")]

    if sections["offerings"]:
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
        data["workflow_steps"] = [
            {
                **_dump(step),
                "checklist": _checklist_ref(step.checklist),
                "notification_rules": [_dump(r) for r in step.ordered_rules],
                "criteria": [_dump(c) for c in step.ordered_criteria],
            }
            for step in call.workflow_steps.select_related("checklist")
            .prefetch_related(
                Prefetch(
                    "notification_rules",
                    queryset=models.CallWorkflowStepNotificationRule.objects.order_by(
                        "trigger", "recipient", "id"
                    ),
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
            .order_by("display_order", "id")
        ]

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
        self.imported = []

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
            checklist_models.Checklist.objects.filter(
                name=name, checklist_type=checklist_type
            ),
            f"{what} checklist '{name}'",
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
        if version != SCHEMA_VERSION:
            raise serializers.ValidationError(
                {"schema_version": f"Unsupported schema version: {version!r}."}
            )

        call = self._import_call()
        if self._section("rounds", "rounds"):
            self._import_rounds(call)
        if self._section("offerings", "requested_offerings"):
            self._import_offerings(call)
        if self._section("workflow_steps", "workflow_steps"):
            self._import_workflow_steps(call)
        for section, configs in _ONE_TO_ONE_CONFIGS.items():
            if self._section(section, *(name for name, _ in configs)):
                self._import_one_to_one(call, section, configs)
        if self._section("role_mappings", "role_mappings"):
            self._import_role_mappings(call)
        if self._section("documents", "documents"):
            self._import_documents(call)
        return call

    def _import_call(self):
        call = models.Call(
            manager=self.manager, created_by=self.user, state=CallStates.DRAFT
        )
        self._assign(call, self.data.get("call"), "call", _CALL_EXCLUDED)
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
            round_obj = self._assign(models.Round(call=call), values, context)
            self._validate(round_obj, context)
            # The rules ProtectedRoundSerializer applies to a round created
            # through the API.
            if round_obj.cutoff_time <= round_obj.start_time:
                raise serializers.ValidationError(
                    {context: "Cutoff time must be later than start time."}
                )
            if any(
                other.start_time < round_obj.cutoff_time
                and other.cutoff_time > round_obj.start_time
                for other in created
            ):
                raise serializers.ValidationError(
                    {context: "Round is overlapping with another round."}
                )
            round_obj.save()
            created.append(round_obj)
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
            count += 1
        self.mark_imported("offerings", count)

    def _import_workflow_steps(self, call):
        steps = []
        entries = self._list(self.data["workflow_steps"], "workflow_steps")
        for index, values in enumerate(entries):
            context = f"workflow_steps[{index}]"
            self._mapping(values, context)
            step_id = self._text(values.get("step"), f"{context}.step", required=True)
            # Creating the call seeded its catalog steps and their default
            # notification rules; the export replaces both.
            step = models.CallWorkflowStep.objects.filter(
                call=call, step=step_id
            ).first() or models.CallWorkflowStep(call=call)
            self._assign(step, values, context)
            step.checklist = self._checklist(
                values.get("checklist"), "Workflow step", f"{context}.checklist"
            )
            # clean() checks dependencies against the other enabled steps, so
            # it runs once all of them are in place.
            self._validate(step, context, run_clean=False)
            step.save()

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
                self._validate(rule, r_context)
                rule.save()

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

        for step, context in steps:
            self._validate(step, context)
        self.mark_imported("workflow_steps", len(steps))

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
        "offering",
        "plan",
        "resource_templates",
        "checklist",
        "notification_rules",
        "criteria",
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
