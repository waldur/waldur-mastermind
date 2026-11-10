"""The names allocation gives the project and resources of an accepted proposal.

A call may name its projects with ``Call.project_name_template``; without one
the project is called ``<call prefix> - <round start> - <proposal name>``, as it
always was. A resource is named by its offering's ``resource_name_pattern``,
the same pattern an ordinary order uses, and otherwise after its project and
offering.

Only the template is formatted. The values put into it -- the proposal name
above all, which the applicant types -- are substituted once and never parsed
again, so a brace in a name is just a brace.
"""

import logging
import string

from waldur_core.core.models import NAME_LENGTH
from waldur_core.structure.models import PROJECT_NAME_LENGTH
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.proposal.enums import (
    DEFAULT_PROJECT_NAME_TEMPLATE,
    PROJECT_NAME_PLACEHOLDERS,
)

logger = logging.getLogger(__name__)


def project_name_template_errors(template: str) -> list[str]:
    """What is wrong with ``template``, or an empty list when nothing is.

    Only the named placeholders are allowed, as plain ``{name}``: no
    positional fields, attribute or index lookups, conversions or format
    specs, which would let a template reach past the values it is given.
    """
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        return [f"Invalid template: {exc}."]
    errors = []
    unknown = []
    for _literal, field, format_spec, conversion in parsed:
        if field is None:
            continue
        if field == "" or field.isdigit():
            errors.append("Positional placeholders such as {} are not allowed.")
        elif field not in PROJECT_NAME_PLACEHOLDERS:
            unknown.append(field)
        elif format_spec or conversion:
            errors.append(
                f"Placeholder {{{field}}} cannot take a conversion or format spec."
            )
    if unknown:
        errors.append(
            "Unknown placeholders: "
            + ", ".join("{%s}" % name for name in sorted(set(unknown)))
            + ". Allowed: "
            + ", ".join("{%s}" % name for name in PROJECT_NAME_PLACEHOLDERS)
            + "."
        )
    if not errors and not template.strip():
        errors.append("The template is blank.")
    return errors


def project_name_context(proposal, today) -> dict[str, str]:
    proposal_round = proposal.round
    call = proposal_round.call
    return {
        "proposal_name": proposal.name or "",
        "proposal_slug": proposal.slug or "",
        "call_prefix": call.backend_id or call.slug or "",
        "call_slug": call.slug or "",
        "call_name": call.name or "",
        "round_start": proposal_round.start_time.strftime("%Y-%m-%d"),
        "round_slug": proposal_round.slug or "",
        "org_slug": call.manager.customer.slug or "",
        "year": str(today.year),
    }


def allocated_project_name(proposal, today) -> str:
    """The name of the project allocating ``proposal`` creates.

    A template that does not render, or renders to nothing, gives the default
    name rather than failing the allocation: the decision has been taken, and
    a project with the old-style name is better than none.
    """
    context = project_name_context(proposal, today)
    template = proposal.round.call.project_name_template
    name = ""
    if template and not project_name_template_errors(template):
        name = template.format_map(context).strip()
    if not name:
        name = DEFAULT_PROJECT_NAME_TEMPLATE.format_map(context)
    return name[:PROJECT_NAME_LENGTH]


def default_resource_name(project_name, offering_name, index=None) -> str:
    """Name a granted resource after its project and offering.

    The offering tells apart the resources of one allocation; a request for an
    offering already requested is numbered. The project part is cut to fit, as
    a project name may be longer than a resource name.
    """
    suffix = f" - {offering_name}" + (f" ({index})" if index else "")
    return (project_name[: max(0, NAME_LENGTH - len(suffix))] + suffix)[:NAME_LENGTH]


def allocated_resource_name(
    project, offering, plan=None, attributes=None, index=None
) -> str:
    """The name of a resource allocation orders on ``offering``.

    An offering with a ``resource_name_pattern`` names it as an ordinary order
    would. A pattern with ``{counter}`` numbers a repeated offering itself;
    one without it would give every item on the offering the same name, so
    the second and later items of the award get ``-<index>`` appended, in the
    pattern's own style. Otherwise the resource is named after its project and
    offering, numbered by ``index`` when the award repeats the offering.
    """
    pattern = (offering.plugin_options or {}).get("resource_name_pattern")
    if pattern:
        try:
            name = marketplace_utils.render_resource_name_pattern(
                pattern, project, offering, plan=plan, attributes=attributes
            )
        except (KeyError, ValueError, IndexError, AttributeError):
            logger.warning(
                "Failed to render resource_name_pattern %r for offering %s; "
                "naming the allocated resource after its project.",
                pattern,
                offering.uuid,
            )
        else:
            if name:
                suffix = ""
                if index and index > 1 and "{counter" not in pattern:
                    suffix = f"-{index}"
                return name[: NAME_LENGTH - len(suffix)] + suffix
    return default_resource_name(project.name, offering.name, index)
