"""What changed between two versions of a call's settings and workflow.

``diff`` compares two call-transfer documents -- a template's, a version
snapshot's or a call's export -- after filling in what a template leaves at
its defaults, so the same configuration written two ways shows no change.

Steps are matched by key: a renamed step is a changed ``name``, never a step
removed and another added. Within a step each field is compared on its own:
values as old and new, sets (the steps it reads, the roles it assigns) as
members added and removed, notification rules by their trigger, recipient,
role and days, criteria by name. A step is reported as moved only when its
place relative to the other steps changed: the steps outside the longest run
keeping their old relative order. Every change is either ``behaviour`` or
``cosmetic`` (names, descriptions and help texts).
"""

import decimal
import functools

from waldur_core.checklist import models as checklist_models
from waldur_mastermind.proposal import workflow_description as wd

BEHAVIOUR = "behaviour"
COSMETIC = "cosmetic"

ADDED = "added"
REMOVED = "removed"
CHANGED = "changed"
MOVED = "moved"

COSMETIC_FIELDS = frozenset(
    {"name", "description", "help_text", "user_guidance", "external_url"}
)
# A step's identity and place: its key matches it, its place is a move.
_STEP_IGNORED = frozenset({"key", "order"})
_STEP_SET_FIELDS = frozenset({"input_steps", "assigns_roles"})
# One-to-one configuration sections, compared flat when both sides have them.
_SECTIONS = (
    "proposal_field_config",
    "applicant_visibility_config",
    "coi_configuration",
    "matching_configuration",
    "assignment_configuration",
)


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, decimal.Decimal)):
        return decimal.Decimal(str(value))
    if isinstance(value, str):
        try:
            return decimal.Decimal(value)
        except decimal.InvalidOperation:
            return None
    return None


def same(old, new) -> bool:
    """Whether two values say the same: "3.0" and 3 do, as do None and ""."""
    if old == new:
        return True
    if old in (None, "") and new in (None, ""):
        return True
    a, b = _number(old), _number(new)
    if a is not None and b is not None:
        return a == b
    return False


# A call's own wording of its mail changes what it says, not what it does.
OVERRIDES_PREFIX = "notification_overrides."


def _kind(field):
    if field in COSMETIC_FIELDS or field.startswith(OVERRIDES_PREFIX):
        return COSMETIC
    return BEHAVIOUR


def _value_change(field, old, new):
    return {
        "field": field,
        "kind": _kind(field),
        "old": old,
        "new": new,
        "added": [],
        "removed": [],
    }


def _set_change(field, old, new):
    old, new = list(old or []), list(new or [])
    added = [v for v in new if v not in old]
    removed = [v for v in old if v not in new]
    if not added and not removed:
        return None
    return {
        "field": field,
        "kind": BEHAVIOUR,
        "old": None,
        "new": None,
        "added": added,
        "removed": removed,
    }


def _flat(old, new, ignored=frozenset(), prefix=""):
    """Field changes between two mappings of plain values."""
    changes = []
    old, new = old or {}, new or {}
    for field in sorted(set(old) | set(new)):
        if field in ignored:
            continue
        a, b = old.get(field), new.get(field)
        if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
            if a != b and not (not a and not b):
                changes.append(_value_change(f"{prefix}{field}", a, b))
            continue
        if not same(a, b):
            changes.append(_value_change(f"{prefix}{field}", a, b))
    return changes


# -- normalising ---------------------------------------------------------------


def _rule_key(rule):
    days = rule.get("days_before")
    return "/".join(
        [
            str(rule.get("trigger") or ""),
            str(rule.get("recipient") or ""),
            str(rule.get("proposal_role") or ""),
            "" if days is None else str(days),
        ]
    )


def _role_names(values):
    return sorted(
        str(v.get("name") if isinstance(v, dict) else v) for v in values or []
    )


def _normalise_step(step):
    values = wd._Describer._normalise_step(step)
    rules = {}
    for rule in values.pop("notification_rules"):
        rule = dict(rule)
        rule["notified_proposal_roles"] = _role_names(
            rule.get("notified_proposal_roles")
        )
        rules[_rule_key(rule)] = rule
    values["notification_rules"] = rules
    # A template leaves a criterion's weight out: it weighs 1.
    values["criteria"] = {
        c.get("name"): {**c, "weight": c.get("weight") or 1}
        for c in (step.get("criteria") or [])
        if isinstance(c, dict)
    }
    checklist = values.get("checklist")
    values["checklist"] = checklist.get("name") if isinstance(checklist, dict) else None
    return values


@functools.cache
def _form_defaults(kind):
    model = {
        "form": checklist_models.Checklist,
        "question": checklist_models.Question,
    }[kind]
    return wd._defaults(model)


def _with_form_defaults(values, kind):
    merged = dict(_form_defaults(kind))
    merged.update({k: v for k, v in values.items() if v is not None})
    return merged


def _normalise_form(form):
    form = _with_form_defaults(form, "form")
    questions = []
    for question in form.get("questions") or []:
        if not isinstance(question, dict):
            continue
        question = _with_form_defaults(question, "question")
        # Options by label, in their order: an import numbers them.
        options = [o for o in (question.get("options") or []) if isinstance(o, dict)]
        question["options"] = [
            o.get("label")
            for _, o in sorted(
                enumerate(options), key=lambda p: (p[1].get("order") or 0, p[0])
            )
        ]
        question["dependencies"] = list(question.get("dependencies") or [])
        questions.append(question)
    form["questions"] = questions
    return form


def _keyed(items, key="key"):
    return {
        item.get(key): item
        for item in (items or [])
        if isinstance(item, dict) and item.get(key) is not None
    }


class _Normalised:
    def __init__(self, document):
        document = document if isinstance(document, dict) else {}
        self.present = set(document)
        self.call = wd._with_defaults(document.get("call") or {}, "call")
        steps = [
            _normalise_step(s)
            for s in (document.get("workflow_steps") or [])
            if isinstance(s, dict)
        ]
        steps.sort(key=lambda s: s.get("order") or 0)
        self.order = [s["key"] for s in steps]
        self.steps = {s["key"]: s for s in steps}
        self.roles = {
            k: wd._with_defaults(v, "role")
            for k, v in _keyed(document.get("proposal_roles")).items()
        }
        self.panels = _keyed(document.get("panels"))
        self.overrides = {
            "/".join([str(o.get("mail_type") or ""), str(o.get("variant") or "")]): o
            for o in (document.get("notification_overrides") or [])
            if isinstance(o, dict)
        }
        self.forms = {
            name: _normalise_form(form)
            for name, form in _keyed(document.get("checklists"), "name").items()
        }
        self.sections = {name: document.get(name) for name in _SECTIONS}
        self.role_mappings = sorted(
            _role_mapping(m)
            for m in (document.get("role_mappings") or [])
            if isinstance(m, dict)
        )
        compliance = document.get("compliance_checklist")
        self.compliance_checklist = (
            compliance.get("name") if isinstance(compliance, dict) else None
        )


def _role_mapping(mapping):
    def name(value):
        return value.get("name") if isinstance(value, dict) else value

    return (
        f"{name(mapping.get('proposal_role'))} -> {name(mapping.get('project_role'))}"
    )


# -- comparing -----------------------------------------------------------------


def _longest_increasing(sequence):
    """Indexes into ``sequence`` of one longest strictly increasing run."""
    if not sequence:
        return set()
    tails, tails_at, previous = [], [], [None] * len(sequence)
    for index, value in enumerate(sequence):
        low, high = 0, len(tails)
        while low < high:
            middle = (low + high) // 2
            if tails[middle] < value:
                low = middle + 1
            else:
                high = middle
        if low > 0:
            previous[index] = tails_at[low - 1]
        if low == len(tails):
            tails.append(value)
            tails_at.append(index)
        else:
            tails[low] = value
            tails_at[low] = index
    kept, index = set(), tails_at[-1]
    while index is not None:
        kept.add(index)
        index = previous[index]
    return kept


def moved_keys(old_order, new_order):
    """Keys, of those in both orders, whose relative place changed."""
    common = [k for k in new_order if k in set(old_order)]
    position = {k: i for i, k in enumerate(old_order)}
    sequence = [position[k] for k in common]
    kept = _longest_increasing(sequence)
    return {k for i, k in enumerate(common) if i not in kept}


def _keyed_changes(field, old, new, ignored=frozenset()):
    """Changes between two mappings of entries -- rules or criteria --
    keyed alike: entries added, removed and changed."""
    changes = []
    for key in [k for k in new if k not in old]:
        changes.append(
            {
                "field": field,
                "kind": BEHAVIOUR,
                "old": None,
                "new": key,
                "added": [key],
                "removed": [],
            }
        )
    for key in [k for k in old if k not in new]:
        changes.append(
            {
                "field": field,
                "kind": BEHAVIOUR,
                "old": key,
                "new": None,
                "added": [],
                "removed": [key],
            }
        )
    for key in [k for k in new if k in old]:
        for change in _flat(old[key], new[key], ignored):
            change["field"] = f"{field}[{key}].{change['field']}"
            changes.append(change)
    return changes


def _step_changes(old, new):
    changes = []
    for field in sorted((set(old) | set(new)) - _STEP_IGNORED):
        a, b = old.get(field), new.get(field)
        if field in _STEP_SET_FIELDS:
            change = _set_change(field, a, b)
            if change:
                changes.append(change)
        elif field == "notification_rules":
            changes.extend(_keyed_changes(field, a or {}, b or {}))
        elif field == "criteria":
            changes.extend(
                _keyed_changes(field, a or {}, b or {}, frozenset({"order"}))
            )
        elif field == "run_condition":
            a, b = a or {}, b or {}
            if (
                a.get("step") != b.get("step")
                or sorted(a.get("outcomes") or []) != sorted(b.get("outcomes") or [])
                or a.get("score_spread_above") != b.get("score_spread_above")
            ):
                changes.append(_value_change(field, a or None, b or None))
        elif isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
            if a != b and (a or b):
                changes.append(_value_change(field, a, b))
        elif not same(a, b):
            changes.append(_value_change(field, a, b))
    return changes


def _entity(key, name, change, fields=(), old_name=None, moved=False):
    fields = list(fields)
    behaviour = (
        change in (ADDED, REMOVED)
        or moved
        or any(f["kind"] == BEHAVIOUR for f in fields)
    )
    return {
        "key": key,
        "name": name,
        "old_name": old_name,
        "change": change,
        "kind": BEHAVIOUR if behaviour else COSMETIC,
        "moved": moved,
        "fields": fields,
    }


def _entities(old, new, order=None, ignored=frozenset()):
    """Changes to keyed entities -- roles, panels -- in ``order``."""
    result = []
    keys = order or list(dict.fromkeys([*new, *old]))
    for key in keys:
        if key in new and key not in old:
            result.append(_entity(key, new[key].get("name") or key, ADDED))
        elif key in old and key not in new:
            result.append(_entity(key, old[key].get("name") or key, REMOVED))
        else:
            fields = _flat(old[key], new[key], ignored | {"key"})
            if fields:
                result.append(
                    _entity(key, new[key].get("name") or key, CHANGED, fields)
                )
    return result


def _form_changes(old, new):
    result = []
    for name in dict.fromkeys([*new, *old]):
        if name in new and name not in old:
            result.append(_entity(name, name, ADDED))
            continue
        if name in old and name not in new:
            result.append(_entity(name, name, REMOVED))
            continue
        a, b = old[name], new[name]
        fields = _flat(a, b, frozenset({"name", "questions"}))
        questions_a = _keyed(a.get("questions"), "description")
        questions_b = _keyed(b.get("questions"), "description")
        fields.extend(
            _keyed_changes(
                "questions",
                questions_a,
                questions_b,
                frozenset({"order", "description"}),
            )
        )
        order_a = [q for q in questions_a if q in questions_b]
        order_b = [q for q in questions_b if q in questions_a]
        if order_a != order_b:
            fields.append(_value_change("question_order", order_a, order_b))
        if fields:
            result.append(_entity(name, name, CHANGED, fields))
    return result


def diff(old_document, new_document) -> dict:
    """The changes from ``old_document`` to ``new_document``."""
    old, new = _Normalised(old_document), _Normalised(new_document)
    moved = moved_keys(old.order, new.order)
    steps = []
    for key in list(dict.fromkeys([*new.order, *old.order])):
        if key in new.steps and key not in old.steps:
            steps.append(_entity(key, new.steps[key].get("name") or key, ADDED))
        elif key in old.steps and key not in new.steps:
            steps.append(_entity(key, old.steps[key].get("name") or key, REMOVED))
        else:
            a, b = old.steps[key], new.steps[key]
            fields = _step_changes(a, b)
            if fields or key in moved:
                steps.append(
                    _entity(
                        key,
                        b.get("name") or key,
                        MOVED if not fields else CHANGED,
                        fields,
                        old_name=a.get("name")
                        if a.get("name") != b.get("name")
                        else None,
                        moved=key in moved,
                    )
                )

    call = _flat(old.call, new.call)
    for section in _SECTIONS:
        if section in old.present and section in new.present:
            call.extend(
                _flat(
                    old.sections[section] or {},
                    new.sections[section] or {},
                    prefix=f"{section}.",
                )
            )
    if (
        "role_mappings" in old.present
        and "role_mappings" in new.present
        and old.role_mappings != new.role_mappings
    ):
        change = _set_change("role_mappings", old.role_mappings, new.role_mappings)
        if change:
            call.append(change)
    if (
        "compliance_checklist" in old.present
        and "compliance_checklist" in new.present
        and old.compliance_checklist != new.compliance_checklist
    ):
        call.append(
            _value_change(
                "compliance_checklist",
                old.compliance_checklist,
                new.compliance_checklist,
            )
        )

    for key in dict.fromkeys([*new.overrides, *old.overrides]):
        field = f"{OVERRIDES_PREFIX}{key}"
        if key not in old.overrides or key not in new.overrides:
            call.append(
                _value_change(
                    field,
                    "customised" if key in old.overrides else None,
                    "customised" if key in new.overrides else None,
                )
            )
            continue
        call.extend(
            _flat(
                old.overrides[key],
                new.overrides[key],
                frozenset({"mail_type", "variant"}),
                prefix=f"{field}.",
            )
        )

    roles = _entities(old.roles, new.roles)
    panels = _entities(old.panels, new.panels, ignored=frozenset({"order"}))
    forms = _form_changes(old.forms, new.forms)

    entities = steps + roles + panels + forms
    summary = {
        "behaviour": sum(1 for e in entities if e["kind"] == BEHAVIOUR)
        + sum(1 for c in call if c["kind"] == BEHAVIOUR),
        "cosmetic": sum(1 for e in entities if e["kind"] == COSMETIC)
        + sum(1 for c in call if c["kind"] == COSMETIC),
        "steps_added": sum(1 for s in steps if s["change"] == ADDED),
        "steps_removed": sum(1 for s in steps if s["change"] == REMOVED),
        "steps_changed": sum(1 for s in steps if s["change"] == CHANGED),
        "steps_moved": sum(1 for s in steps if s["moved"]),
    }
    return {
        "steps": steps,
        "call": call,
        "proposal_roles": roles,
        "panels": panels,
        "forms": forms,
        "summary": summary,
    }


def changed_step_keys(changes, behaviour_only=True) -> list[str]:
    """Keys of the steps ``changes`` touches."""
    return [
        s["key"]
        for s in changes["steps"]
        if not behaviour_only or s["kind"] == BEHAVIOUR
    ]
