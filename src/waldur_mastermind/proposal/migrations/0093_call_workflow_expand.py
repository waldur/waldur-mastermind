"""The call workflow, part 1 of 3: everything it adds to the schema.

Every new table and column of the configurable call workflow, in one
migration; 0094 fills in what existing calls and proposals need, and 0095 then
makes it required and unique and drops what is no longer read.

Django spends far longer here rebuilding its picture of the models after each
operation than on the SQL, so the operations are as few as they can be: each
new model is created whole, with its fields and constraints.

An existing table is locked from its first operation here until the migration
commits, while the previous release still serves requests. So the new tables
come first, then the columns of existing ones, and last the two tables read
outside the proposal module: calls, for every user's visible organisations,
and requested offerings, for the marketplace's open-call filters.
"""

import django.core.validators
import django.db.models.constraints
import django.db.models.deletion
import django.utils.timezone
import django_fsm
import model_utils.fields
from django.conf import settings
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.logging.mixins


class Migration(migrations.Migration):
    dependencies = [
        ("checklist", "0017_checklist_organization"),
        ("marketplace", "0324_resource_api_key_description"),
        ("permissions", "0038_invoice_list_permission"),
        ("proposal", "0092_delete_empty_proposal_documents"),
        ("structure", "0088_backfill_project_initial_revisions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CallNotificationOverride",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                ("mail_type", models.CharField(max_length=64)),
                ("variant", models.CharField(blank=True, default="", max_length=32)),
                ("subject", models.TextField()),
                ("body_text", models.TextField()),
                ("body_html", models.TextField(blank=True, default="")),
                ("enabled", models.BooleanField(default=True)),
                (
                    "call",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notification_overrides",
                        to="proposal.call",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["mail_type", "variant", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("call", "mail_type", "variant"),
                        name="proposal_unique_call_notification_override",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="CallOfferingComponent",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "component_type",
                    models.CharField(
                        help_text="Type of the offering component: one billed by its limit that the call offering's plan prices.",
                        max_length=50,
                    ),
                ),
                (
                    "min_per_proposal",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text="The least a proposal may request. Empty: no minimum.",
                        max_digits=20,
                        null=True,
                        validators=[django.core.validators.MinValueValidator(0)],
                    ),
                ),
                (
                    "max_per_proposal",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text="The most a proposal may request. Empty: no maximum.",
                        max_digits=20,
                        null=True,
                        validators=[django.core.validators.MinValueValidator(0)],
                    ),
                ),
                (
                    "is_fixed",
                    models.BooleanField(
                        default=False,
                        help_text="Every proposal requests exactly default_amount; the applicant cannot change it.",
                    ),
                ),
                (
                    "default_amount",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text="The amount a request starts from; with is_fixed, the amount every proposal requests.",
                        max_digits=20,
                        null=True,
                        validators=[django.core.validators.MinValueValidator(0)],
                    ),
                ),
                (
                    "limit_period",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="The component's limit period when the call took the rule in. Pools count awards by it, so a provider changing the period later does not recount what the call already awarded.",
                        max_length=30,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "requested_offering",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="component_rules",
                        to="proposal.requestedoffering",
                    ),
                ),
            ],
            options={
                "verbose_name": "Call offering component",
                "verbose_name_plural": "Call offering components",
                "ordering": ["created", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("requested_offering", "component_type"),
                        name="proposal_unique_call_offering_component",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="CallPanel",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "key",
                    models.SlugField(
                        help_text="Identifier of the panel within its call.",
                        max_length=64,
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True)),
                (
                    "order",
                    models.PositiveSmallIntegerField(
                        default=0, help_text="Position of the panel in the call's list."
                    ),
                ),
                (
                    "call",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="panels",
                        to="proposal.call",
                    ),
                ),
                (
                    "chair",
                    models.ForeignKey(
                        blank=True,
                        help_text="The chair of the panel, one of its members.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "members",
                    models.ManyToManyField(
                        blank=True,
                        help_text="Panel members of the call who sit on this panel.",
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "science_domains",
                    models.ManyToManyField(
                        blank=True,
                        help_text="Proposals of these domains are routed to the panel.",
                        related_name="+",
                        to="structure.sciencedomain",
                    ),
                ),
                (
                    "science_sub_domains",
                    models.ManyToManyField(
                        blank=True,
                        help_text="Proposals of these sub-domains are routed to the panel, whichever panel their domain maps to.",
                        related_name="+",
                        to="structure.sciencesubdomain",
                    ),
                ),
            ],
            options={
                "verbose_name": "Call panel",
                "verbose_name_plural": "Call panels",
                "ordering": ["order", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("call", "key"), name="proposal_unique_call_panel_key"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="CallProposalRole",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "key",
                    models.SlugField(
                        help_text="Identifier of the role within its call.",
                        max_length=64,
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True)),
                (
                    "eligible_from",
                    models.CharField(
                        choices=[
                            ("call_role:manager", "Call managers of the call"),
                            ("call_role:reviewer", "Reviewers of the call"),
                            ("call_role:panel_member", "Panel members of the call"),
                            (
                                "proposal_reviewers",
                                "Reviewers who submitted a review of the proposal",
                            ),
                            ("any_user", "Any user"),
                            ("panel:chair", "The chair of the proposal's panel"),
                            ("panel:member", "Members of the proposal's panel"),
                        ],
                        default="call_role:panel_member",
                        help_text="Who may hold the role.",
                        max_length=32,
                    ),
                ),
                (
                    "min_holders",
                    models.PositiveSmallIntegerField(
                        default=1,
                        help_text="How many holders an assignment step for this role needs before it can complete.",
                        validators=[django.core.validators.MinValueValidator(1)],
                    ),
                ),
                (
                    "max_holders",
                    models.PositiveSmallIntegerField(
                        blank=True,
                        help_text="How many people may hold the role on one proposal. Empty: no limit.",
                        null=True,
                        validators=[django.core.validators.MinValueValidator(1)],
                    ),
                ),
                (
                    "allows_lead",
                    models.BooleanField(
                        default=False,
                        help_text="Whether one holder is marked as the lead.",
                    ),
                ),
                (
                    "call",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="proposal_roles",
                        to="proposal.call",
                    ),
                ),
            ],
            options={
                "verbose_name": "Evaluator role",
                "verbose_name_plural": "Evaluator roles",
                "ordering": ["created", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("call", "key"),
                        name="proposal_unique_call_proposal_role_key",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="CallReviewerEligibility",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("user_affiliations", models.JSONField(blank=True, default=list)),
                ("user_email_patterns", models.JSONField(blank=True, default=list)),
                (
                    "user_identity_sources",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="List of allowed identity sources (identity providers).",
                    ),
                ),
                (
                    "user_nationalities",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="List of allowed nationality codes (ISO 3166-1 alpha-2). User must have one of these.",
                    ),
                ),
                (
                    "user_organization_types",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="List of allowed organization type URNs (SCHAC). User must match one.",
                    ),
                ),
                (
                    "user_assurance_levels",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="List of required assurance URIs. User must have ALL of these.",
                    ),
                ),
                (
                    "call",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reviewer_eligibility",
                        to="proposal.call",
                    ),
                ),
            ],
            options={
                "verbose_name": "Call reviewer eligibility",
            },
        ),
        migrations.CreateModel(
            name="CallTemplate",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                ("name", models.CharField(max_length=150, unique=True)),
                ("description", models.TextField(blank=True)),
                ("document", models.JSONField()),
                ("schema_version", models.PositiveSmallIntegerField()),
                ("is_published", models.BooleanField(default=False)),
                (
                    "version",
                    models.PositiveIntegerField(
                        default=1,
                        editable=False,
                        help_text="Incremented whenever the document changes; name, description and publication leave it as it is.",
                    ),
                ),
                (
                    "scenarios",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="Named paths through the workflow, each a dry run with what must hold at its end. They run on the calls created from the template.",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["name", "id"],
            },
        ),
        migrations.CreateModel(
            name="ProposalRun",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "phase",
                    models.CharField(
                        choices=[
                            ("evaluation", "Evaluation"),
                            ("extension", "Extension"),
                            ("closing", "Closing"),
                        ],
                        help_text="The post-award phase the run walks: extension or closing.",
                        max_length=16,
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("open", "Open"),
                            ("approved", "Approved"),
                            ("rejected", "Rejected"),
                            ("withdrawn", "Withdrawn"),
                            ("expired", "Expired"),
                        ],
                        db_index=True,
                        default="open",
                        max_length=16,
                    ),
                ),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "requested_end_date",
                    models.DateField(
                        blank=True,
                        help_text="Extension: the end date the applicant asks for.",
                        null=True,
                    ),
                ),
                (
                    "requested_limits",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Extension: the limits the applicant asks for, per resource.",
                    ),
                ),
                (
                    "granted_end_date",
                    models.DateField(
                        blank=True,
                        help_text="Extension: the end date the decision grants.",
                        null=True,
                    ),
                ),
                (
                    "granted_limits",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Extension: the limits the decision grants, per resource.",
                    ),
                ),
                (
                    "applied_changes",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Extension: what the approval changed -- the end date requests recorded and the orders created -- and what it left alone, and why.",
                    ),
                ),
                (
                    "granted_by",
                    models.ForeignKey(
                        blank=True,
                        help_text="Extension: who last set what the decision grants.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "opened_by",
                    models.ForeignKey(
                        blank=True,
                        help_text="Who opened the run; empty when the system opened it.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "proposal",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="runs",
                        to="proposal.proposal",
                    ),
                ),
            ],
            options={
                "ordering": ["created", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("state", "open")),
                        fields=("proposal",),
                        name="proposal_unique_open_run",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="ReviewerPublicationRequest",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "state",
                    django_fsm.FSMIntegerField(
                        choices=[
                            (1, "draft"),
                            (2, "pending"),
                            (3, "approved"),
                            (4, "rejected"),
                            (5, "canceled"),
                        ],
                        default=1,
                    ),
                ),
                (
                    "reviewed_at",
                    models.DateTimeField(
                        blank=True,
                        editable=False,
                        help_text="Timestamp when the review was completed",
                        null=True,
                    ),
                ),
                (
                    "review_comment",
                    models.TextField(
                        blank=True,
                        help_text="Optional comment provided during review",
                        null=True,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="publication_requests",
                        to="proposal.reviewerprofile",
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        help_text="User who performed the review",
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Reviewer publication request",
                "ordering": ["-created", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("state", 2)),
                        fields=("profile",),
                        name="unique_pending_reviewer_publication_request",
                    )
                ],
            },
            bases=(models.Model, waldur_core.logging.mixins.LoggableMixin),
        ),
        migrations.CreateModel(
            name="RoundMeetingNote",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                ("title", models.CharField(max_length=255)),
                ("body", models.TextField(blank=True)),
                (
                    "submitted_at",
                    models.DateTimeField(blank=True, editable=False, null=True),
                ),
                (
                    "author",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "call_step",
                    models.ForeignKey(
                        blank=True,
                        help_text="The round step this note reports on; empty for a free note.",
                        null=True,
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="round_reports",
                        to="proposal.callworkflowstep",
                    ),
                ),
                (
                    "proposals",
                    models.ManyToManyField(
                        blank=True,
                        help_text="The proposals a round report covers; fixed once submitted.",
                        related_name="round_reports",
                        to="proposal.proposal",
                    ),
                ),
                (
                    "round",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="meeting_notes",
                        to="proposal.round",
                    ),
                ),
            ],
            options={
                "ordering": ["-created", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(
                            ("author__isnull", False), ("call_step__isnull", False)
                        ),
                        fields=("round", "call_step", "author"),
                        name="proposal_round_report_unique_per_author",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="RoundStep",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "due_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="When the step is due in this round, for every proposal. Empty: the step's duration counts from when it starts.",
                        null=True,
                    ),
                ),
                (
                    "opened_at",
                    models.DateTimeField(blank=True, editable=False, null=True),
                ),
                (
                    "opened_reason",
                    models.TextField(
                        blank=True,
                        editable=False,
                        help_text="Why the step was opened while proposals had not reached it.",
                    ),
                ),
                (
                    "sent_notifications",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="Round-level mail already sent for this step, by key.",
                    ),
                ),
                (
                    "call_step",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="round_steps",
                        to="proposal.callworkflowstep",
                    ),
                ),
                (
                    "opened_by",
                    models.ForeignKey(
                        blank=True,
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "round",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="steps",
                        to="proposal.round",
                    ),
                ),
            ],
            options={
                "ordering": ["round", "call_step__order", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("round", "call_step"), name="proposal_unique_round_step"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="StepContribution",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "is_lead",
                    models.BooleanField(
                        default=False,
                        help_text="Whether the contributor leads the step's work.",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("accepted", "Accepted"),
                            ("submitted", "Submitted"),
                            ("declined", "Declined"),
                            ("withdrawn", "Withdrawn"),
                            ("expired", "Expired"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=16,
                    ),
                ),
                (
                    "outcome",
                    models.CharField(
                        blank=True,
                        help_text="What the contribution concludes, e.g. conditionally_accepted.",
                        max_length=32,
                    ),
                ),
                (
                    "conditions",
                    models.TextField(
                        blank=True, help_text="Conditions of a conditional acceptance."
                    ),
                ),
                ("comment", models.TextField(blank=True)),
                (
                    "due_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="When the contribution is due; the step's deadline by default.",
                        null=True,
                    ),
                ),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                (
                    "delegated_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="When the slot was handed to its delegate.",
                        null=True,
                    ),
                ),
                (
                    "coi_declaration_text",
                    models.TextField(
                        blank=True,
                        help_text="The conflict-of-interest declaration the delegate confirmed on taking the slot over, as worded then; empty for the platform's standard one, or when the step asks for none.",
                    ),
                ),
                (
                    "assignment_item",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="contribution",
                        to="proposal.assignmentitem",
                    ),
                ),
                (
                    "contributor",
                    models.ForeignKey(
                        blank=True,
                        help_text="Who the contribution belongs to; for a per-offering slot, who submitted it.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "delegate",
                    models.ForeignKey(
                        blank=True,
                        help_text="Who the slot was handed to, as the step's delegable_to allows: while they stay eligible, they alone fill it once they accept (the slot is then accepted). The slot's holders keep reading it.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "delegated_by",
                    models.ForeignKey(
                        blank=True,
                        help_text="Who handed the slot to its delegate.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "proposal_step",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="contributions",
                        to="proposal.proposalworkflowstepinstance",
                    ),
                ),
                (
                    "requested_offering",
                    models.ForeignKey(
                        blank=True,
                        help_text="The offering this slot assesses, for a per-offering slot.",
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="step_contributions",
                        to="proposal.requestedoffering",
                    ),
                ),
                (
                    "review",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="contribution",
                        to="proposal.review",
                    ),
                ),
            ],
            options={
                "verbose_name": "Step contribution",
                "verbose_name_plural": "Step contributions",
                "ordering": ["created", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("requested_offering__isnull", False)),
                        fields=("proposal_step", "requested_offering"),
                        name="proposal_unique_contribution_per_requested_offering",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(
                            ("contributor__isnull", False),
                            ("requested_offering__isnull", True),
                            ("review__isnull", True),
                        ),
                        fields=("proposal_step", "contributor"),
                        name="proposal_unique_person_contribution_per_step",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="CallResourcePool",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "offered_total",
                    models.DecimalField(
                        decimal_places=2,
                        help_text="How much the call awards from the pool, in the component's unit over the whole length of each award.",
                        max_digits=20,
                        validators=[django.core.validators.MinValueValidator(0)],
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "offering_component",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pools",
                        to="proposal.callofferingcomponent",
                    ),
                ),
                (
                    "round",
                    models.ForeignKey(
                        blank=True,
                        help_text="The round the pool covers. Empty: the whole call.",
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="resource_pools",
                        to="proposal.round",
                    ),
                ),
            ],
            options={
                "verbose_name": "Call resource pool",
                "verbose_name_plural": "Call resource pools",
                "ordering": ["created", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("round__isnull", False)),
                        fields=("offering_component", "round"),
                        name="proposal_unique_round_resource_pool",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("round__isnull", True)),
                        fields=("offering_component",),
                        name="proposal_unique_call_resource_pool",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ProposalChangeRequest",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "message",
                    models.TextField(
                        help_text="What is to change, for the applicant, or for the contributor of a reopened contribution."
                    ),
                ),
                (
                    "unlocked_parts",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="The parts of the proposal the applicant may edit while the request is open: field:<name>, documents, resources, team, compliance.",
                    ),
                ),
                (
                    "due_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="When the changes are due. Past it, the request lapses and the step returns to its actor.",
                        null=True,
                    ),
                ),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "resolution",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("submitted", "Submitted"),
                            ("lapsed", "Lapsed"),
                            ("withdrawn", "Withdrawn"),
                            ("cancelled", "Cancelled"),
                        ],
                        help_text="How the request was closed; empty while it is open.",
                        max_length=16,
                    ),
                ),
                (
                    "closed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "proposal",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="change_requests",
                        to="proposal.proposal",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "step_instance",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="change_requests",
                        to="proposal.proposalworkflowstepinstance",
                    ),
                ),
                (
                    "contribution",
                    models.ForeignKey(
                        blank=True,
                        help_text="The contribution reopened; empty for a request to the applicant.",
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="change_requests",
                        to="proposal.stepcontribution",
                    ),
                ),
            ],
            options={
                "verbose_name": "Proposal change request",
                "verbose_name_plural": "Proposal change requests",
                "ordering": ["created", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(
                            ("closed_at__isnull", True), ("contribution__isnull", True)
                        ),
                        fields=("step_instance",),
                        name="proposal_one_open_change_request_per_step",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(
                            ("closed_at__isnull", True), ("contribution__isnull", False)
                        ),
                        fields=("contribution",),
                        name="proposal_one_open_change_request_per_contribution",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ProposalRanking",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "rank",
                    models.PositiveIntegerField(
                        help_text="Position of the proposal in its round, 1 being the first.",
                        validators=[django.core.validators.MinValueValidator(1)],
                    ),
                ),
                (
                    "final_score",
                    models.DecimalField(
                        blank=True,
                        decimal_places=3,
                        help_text="The committee's final score for the proposal.",
                        max_digits=10,
                        null=True,
                    ),
                ),
                (
                    "recommended_resources",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text='Recommended amounts per requested offering and component: {"<requested offering uuid>": {"<component type>": amount}}.',
                    ),
                ),
                ("notes", models.TextField(blank=True)),
                ("scope_key", models.PositiveIntegerField(default=0, editable=False)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "proposal",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rankings",
                        to="proposal.proposal",
                    ),
                ),
                (
                    "round",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rankings",
                        to="proposal.round",
                    ),
                ),
                (
                    "meeting_note",
                    models.ForeignKey(
                        blank=True,
                        help_text="The round report this ranking belongs to, such as a domain panel's ranking within its domain. Empty for the round's overall ranking.",
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rankings",
                        to="proposal.roundmeetingnote",
                    ),
                ),
            ],
            options={
                "ordering": ["round", "rank", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("round", "scope_key", "proposal"),
                        name="proposal_ranking_unique_per_round_and_proposal",
                    ),
                    models.UniqueConstraint(
                        deferrable=django.db.models.constraints.Deferrable["DEFERRED"],
                        fields=("round", "scope_key", "rank"),
                        name="proposal_ranking_unique_rank_per_round",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ProposalReviewerSuggestion",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "suggested_email",
                    models.EmailField(
                        blank=True,
                        help_text="E-mail address of a suggested reviewer without a user account.",
                        max_length=254,
                    ),
                ),
                (
                    "note",
                    models.TextField(
                        blank=True, help_text="Why this reviewer: expertise."
                    ),
                ),
                ("invited_at", models.DateTimeField(blank=True, null=True)),
                ("discarded_at", models.DateTimeField(blank=True, null=True)),
                (
                    "discarded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "invited_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "pool_entry",
                    models.ForeignKey(
                        blank=True,
                        help_text="The pool invitation the suggestion was turned into.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="proposal_reviewer_suggestions",
                        to="proposal.callreviewerpool",
                    ),
                ),
                (
                    "proposal",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reviewer_suggestions_by_evaluators",
                        to="proposal.proposal",
                    ),
                ),
                (
                    "suggested_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "contribution",
                    models.ForeignKey(
                        help_text="The suggester's contribution to the step the suggestion came from.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reviewer_suggestions",
                        to="proposal.stepcontribution",
                    ),
                ),
            ],
            options={
                "verbose_name": "Proposal reviewer suggestion",
                "verbose_name_plural": "Proposal reviewer suggestions",
                "ordering": ["created", "id"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("suggested_user__isnull", False),
                            ("suggested_email__gt", ""),
                            _connector="OR",
                        ),
                        name="proposal_reviewer_suggestion_user_or_email",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("suggested_user__isnull", False)),
                        fields=("contribution", "suggested_user"),
                        name="proposal_unique_suggested_user_per_contribution",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(
                            ("suggested_email__gt", ""),
                            ("suggested_user__isnull", True),
                        ),
                        fields=("contribution", "suggested_email"),
                        name="proposal_unique_suggested_email_per_contribution",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ProposalRoleAssignment",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                ("is_lead", models.BooleanField(default=False)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "revoked_reason",
                    models.CharField(
                        blank=True,
                        help_text="Why the role was taken away, when not by hand.",
                        max_length=255,
                    ),
                ),
                (
                    "coi_confirmation_required",
                    models.BooleanField(
                        default=False,
                        help_text="The holder confirms having no conflict of interest before gaining access to the proposal.",
                    ),
                ),
                ("coi_confirmed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "coi_declaration_only",
                    models.BooleanField(
                        default=False,
                        help_text="The holder had no reviewer profile, so no recorded conflict could be checked: their own declaration is all there is.",
                    ),
                ),
                (
                    "coi_declaration_text",
                    models.TextField(
                        blank=True,
                        help_text="The declaration the holder confirmed, as worded when they did; empty for the platform's standard one.",
                    ),
                ),
                (
                    "assigned_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "assigned_in",
                    models.ForeignKey(
                        blank=True,
                        help_text="The assignment step the holder was picked in, if any.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="role_assignments",
                        to="proposal.proposalworkflowstepinstance",
                    ),
                ),
                (
                    "proposal",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="role_assignments",
                        to="proposal.proposal",
                    ),
                ),
                (
                    "revoked_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "role",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="assignments",
                        to="proposal.callproposalrole",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Evaluator role assignment",
                "verbose_name_plural": "Evaluator role assignments",
                "ordering": ["created", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("revoked_at__isnull", True)),
                        fields=("proposal", "user"),
                        name="proposal_unique_active_role_assignment",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(
                            ("is_lead", True), ("revoked_at__isnull", True)
                        ),
                        fields=("proposal", "role"),
                        name="proposal_unique_active_role_lead",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="RoundMeetingNoteDocument",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                ("file", models.FileField(upload_to="proposal_round_meeting_notes")),
                (
                    "note",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="documents",
                        to="proposal.roundmeetingnote",
                    ),
                ),
            ],
            options={
                "ordering": ["created", "id"],
            },
        ),
        migrations.CreateModel(
            name="StepContributionCoiConfirmation",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "confirmed_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                (
                    "declaration_text",
                    models.TextField(
                        blank=True,
                        help_text="The declaration confirmed, as worded then; empty for the platform's standard one.",
                    ),
                ),
                (
                    "declaration_source",
                    models.CharField(
                        choices=[
                            ("step", "Step"),
                            ("call", "Call"),
                            ("platform", "Platform"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "contribution",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="coi_confirmations",
                        to="proposal.stepcontribution",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        help_text="Who confirmed.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Contribution conflict-of-interest confirmation",
                "verbose_name_plural": "Contribution conflict-of-interest confirmations",
                "ordering": ["confirmed_at", "id"],
            },
        ),
        migrations.CreateModel(
            name="WorkflowVersion",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("number", models.PositiveIntegerField()),
                ("document", models.JSONField()),
                ("document_hash", models.CharField(max_length=64)),
                (
                    "reason",
                    models.CharField(
                        choices=[
                            ("created", "Created"),
                            ("created_from_template", "Created from a template"),
                            ("imported", "Imported"),
                            ("duplicated", "Duplicated"),
                            ("edited", "Edited"),
                            ("activated", "Activated"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "summary",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Counts of the changes from the previous version.",
                    ),
                ),
                (
                    "checks",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Results of checks run on this version.",
                    ),
                ),
                (
                    "call",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="workflow_versions",
                        to="proposal.call",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "template",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="versions",
                        to="proposal.calltemplate",
                    ),
                ),
            ],
            options={
                "ordering": ["number", "id"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("call__isnull", False), ("template__isnull", True)
                            ),
                            models.Q(
                                ("call__isnull", True), ("template__isnull", False)
                            ),
                            _connector="OR",
                        ),
                        name="proposal_workflow_version_one_owner",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("call__isnull", False)),
                        fields=("call", "number"),
                        name="proposal_unique_call_workflow_version",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("template__isnull", False)),
                        fields=("template", "number"),
                        name="proposal_unique_template_workflow_version",
                    ),
                ],
            },
        ),
        migrations.RenameField(
            model_name="callworkflowstep",
            old_name="step",
            new_name="key",
        ),
        migrations.RenameField(
            model_name="callworkflowstep",
            old_name="responsible_role",
            new_name="completer",
        ),
        migrations.RenameField(
            model_name="callworkflowstep",
            old_name="min_reviewers",
            new_name="min_contributions",
        ),
        migrations.AlterModelOptions(
            name="callworkflowstep",
            options={
                "ordering": ["order", "id"],
                "verbose_name": "Workflow step",
                "verbose_name_plural": "Workflow steps",
            },
        ),
        migrations.AlterModelOptions(
            name="reviewerprofile",
            options={
                "ordering": ["created", "id"],
                "verbose_name": "Reviewer profile",
                "verbose_name_plural": "Reviewer profiles",
            },
        ),
        migrations.RemoveConstraint(
            model_name="proposalworkflowstepinstance",
            name="unique_active_workflow_step_per_proposal",
        ),
        migrations.AlterUniqueTogether(
            name="callworkflowstep",
            unique_together=set(),
        ),
        migrations.AlterUniqueTogether(
            name="callworkflowstepnotificationrule",
            unique_together=set(),
        ),
        migrations.AlterUniqueTogether(
            name="proposalworkflowstepinstance",
            unique_together=set(),
        ),
        migrations.AddField(
            model_name="assignmentbatch",
            name="call_step",
            field=models.ForeignKey(
                blank=True,
                help_text="The step the reviews accepted from this batch contribute to. The call's review step when empty.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assignment_batches",
                to="proposal.callworkflowstep",
            ),
        ),
        migrations.AddField(
            model_name="awardedresource",
            name="modified_by",
            field=models.ForeignKey(
                blank=True,
                help_text="Who last changed the item. Empty for one nobody changed since it was copied from the request or added.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="awardedresource",
            name="provision_error",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Why allocation could not order the item -- the offering stopped taking orders, its plan was archived, its limits no longer fit. Empty once it is provisioned.",
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="allows_change_requests",
            field=models.BooleanField(
                default=False,
                help_text="Whoever acts on the step may send the proposal back to the applicant to change the parts they unlock, as often as needed. The proposal stays in review on the step meanwhile. Not for the allocation decision nor an applicant step.",
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="allows_conditional_acceptance",
            field=models.BooleanField(
                default=True,
                help_text="Per-offering assessment: a contribution may accept a requested offering under conditions. Off, it accepts or rejects it.",
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="coi_declaration_text",
            field=models.TextField(
                blank=True,
                help_text="The conflict-of-interest declaration this step's evaluators confirm; empty for the call's, or the platform's standard one.",
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="collects_reviewer_suggestions",
            field=models.BooleanField(
                default=False,
                help_text="Assessment step of evaluator-role holders: each holder may suggest reviewers for the proposal while the step is active. The call's managers turn suggestions into pool invitations.",
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="contributor_source",
            field=models.CharField(
                default="none",
                help_text="Who contributes to the step, each with a contribution of their own: none, requested_offering_team, assigned_reviewers, proposal_role:<id> or call_role:<role>.",
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="contributors_lead_only",
            field=models.BooleanField(
                default=False,
                help_text="Only the lead of the evaluator role the contributor source names contributes: one slot, the lead's, which moves when the lead changes. The step waits while the role has no lead.",
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="decides_automatically",
            field=models.BooleanField(
                default=False,
                help_text="Allocation decision: approve on reaching this step, as the system, with the award as requested; fall back to the completer when the approval is refused.",
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="delegable_to",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Who a contribution slot of the step can be handed to by its holder or the call's managers: empty for nobody, offering_team for anyone with a role on the slot's own offering (per-offering steps), or proposal_role:<id> for the holders of an evaluator role on the proposal (per-person steps). The person it is handed to accepts or declines it.",
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="description",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="input_steps",
            field=models.ManyToManyField(
                blank=True,
                help_text="Earlier steps of the call whose outputs -- contributions, reviews, form answers and outcome -- this step's contributors and completer read. On an applicant step, the applicant reads their public parts.",
                related_name="output_steps",
                to="proposal.callworkflowstep",
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="is_allocation_decision",
            field=models.BooleanField(
                default=False,
                help_text="The decision step that settles the award. A call is activated with exactly one enabled.",
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="kind",
            field=models.CharField(
                choices=[
                    ("applicant_action", "Applicant action"),
                    ("assignment", "Assignment"),
                    ("assessment", "Assessment"),
                    ("decision", "Decision"),
                ],
                default="",
                help_text="Behavior of the step.",
                max_length=32,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="max_contributions",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Most contributions the step accepts; no limit when empty.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="name",
            field=models.CharField(default="", max_length=255),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="on_negative",
            field=models.CharField(
                choices=[
                    ("reject", "Reject the proposal"),
                    ("cancel", "Cancel the proposal"),
                    ("continue", "Continue to the next step"),
                ],
                default="reject",
                help_text="What a negative outcome does to the proposal.",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="order",
            field=models.PositiveIntegerField(
                help_text="Position of the step in the call's workflow.", null=True
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="outcome_set",
            field=models.CharField(
                choices=[
                    ("eligibility", "Eligible / ineligible"),
                    ("feasibility", "Feasible / infeasible"),
                    ("reviewed", "Reviewed"),
                    ("approval", "Approved / declined"),
                    ("acceptance", "Accepted / declined"),
                    ("submitted", "Submitted"),
                    ("done", "Done"),
                    ("per_offering", "Per requested offering"),
                ],
                default="",
                help_text="Outcomes the step can be completed with.",
                max_length=32,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="parallel_with_previous",
            field=models.BooleanField(
                default=False,
                help_text="Start together with the step before it, as one stage: every step of a stage starts at once, and the next stage starts when all of them have ended.",
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="per_requested_offering",
            field=models.BooleanField(
                default=False,
                help_text="One contribution per offering the proposal requests, which any member of that offering's team may fill.",
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="phase",
            field=models.CharField(
                choices=[
                    ("evaluation", "Evaluation"),
                    ("extension", "Extension"),
                    ("closing", "Closing"),
                ],
                default="evaluation",
                help_text="When the step runs: in the evaluation, from submission to the award; in an extension, which the applicant of an accepted proposal asks for as often as needed; or in the closing, the final report at the end of the allocation. A phase's steps run in their order, each extension and closing in a run of its own.",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="project_end_offset_days",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="First step of an extension or closing phase: days before the project's end date. An extension can be asked for until then; the closing opens then (0: on the end date).",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="release_to_applicant",
            field=models.BooleanField(
                default=False,
                help_text="Assessment step: once completed, its report can be released to the applicant, such as a consolidated report or final feedback.",
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="round_scoped",
            field=models.BooleanField(
                default=False,
                help_text="Run the step once per round rather than per proposal, such as a panel meeting after the cut-off: the call's managers open it for the round's proposals together, its contributors write one round report each covering their proposals, and it is completed for the round, never on one proposal.",
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="run_condition",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='When the step runs: empty to run on every proposal, {"step": <key of an earlier step>, "outcomes": [...]} to run only when that step ended with one of the outcomes, or one of its contributions did, or {"step": <key of an earlier step>, "score_spread_above": <points>} to run only when the scores its contributions gave the proposal lie more than that many points apart. Otherwise the proposal skips it.',
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="status_labels",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="What each audience's dashboard calls the step, as entered: {\"applicant\": {...}, \"evaluators\": {...}, \"call_team\": {...}, \"offering_team\": {...}}, each with optional 'active', 'completed' and 'changes_requested' texts and 'outcomes' ({<outcome>: text}). Left out, the computed label stays; the call team's and the offering teams' fall back to the evaluators'. The applicant's apply only while the step is visible to the applicant. Other labels may show counts of the step's work: {invited}, {accepted}, {declined}, {submitted}, {expected}, {reports_submitted}, {reports_expected}.",
            ),
        ),
        migrations.AddField(
            model_name="proposal",
            name="awarded_duration_months",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="The project length the allocation decision awards, in months: the requested one until the call manager picks another listed one.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="proposal",
            name="completed_at",
            field=models.DateTimeField(
                blank=True,
                editable=False,
                help_text="When the accepted proposal's project was completed: its final report accepted. No run can be opened afterwards.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="proposal",
            name="completed_by",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="proposal",
            name="current_step",
            field=models.ForeignKey(
                blank=True,
                help_text="The proposal's step in its workflow: the active one, or a completed one waiting to be moved on by hand.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="proposal.proposalworkflowstepinstance",
            ),
        ),
        migrations.AddField(
            model_name="proposal",
            name="requested_duration_months",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="The project length the applicant chose, in months, from the call's allowed_duration_months.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="proposaldocumentation",
            name="uploaded_by",
            field=models.ForeignKey(
                blank=True,
                help_text="Who attached the document.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="proposalworkflowstepinstance",
            name="call_step",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="proposal_instances",
                to="proposal.callworkflowstep",
            ),
        ),
        migrations.AddField(
            model_name="proposalworkflowstepinstance",
            name="feedback_withheld",
            field=models.BooleanField(
                default=False,
                help_text="The outcome reason is feedback on a step whose report is released to the applicant; the applicant reads it only once the report is released.",
            ),
        ),
        migrations.AddField(
            model_name="proposalworkflowstepinstance",
            name="input_steps",
            field=models.ManyToManyField(
                blank=True,
                help_text="The call steps whose outputs this step reads on the proposal.",
                related_name="+",
                to="proposal.callworkflowstep",
            ),
        ),
        migrations.AddField(
            model_name="proposalworkflowstepinstance",
            name="is_allocation_decision",
            field=models.BooleanField(
                default=False,
                help_text="Whether this step settles the proposal's award.",
            ),
        ),
        migrations.AddField(
            model_name="proposalworkflowstepinstance",
            name="order",
            field=models.PositiveIntegerField(
                help_text="Position of the step in this proposal's path.", null=True
            ),
        ),
        migrations.AddField(
            model_name="proposalworkflowstepinstance",
            name="released_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the step's report was released to the applicant.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="proposalworkflowstepinstance",
            name="released_by",
            field=models.ForeignKey(
                blank=True,
                help_text="Who released the step's report to the applicant.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="proposalworkflowstepinstance",
            name="released_text",
            field=models.TextField(
                blank=True,
                help_text="The text the applicant reads in place of the step's report, as edited when it was released; the report itself when empty.",
            ),
        ),
        migrations.AddField(
            model_name="proposalworkflowstepinstance",
            name="run_condition",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="The call step's run condition when the proposal was submitted; empty to run on every proposal.",
            ),
        ),
        migrations.AddField(
            model_name="proposalworkflowstepinstance",
            name="stage",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="The stage the step runs in on this proposal: the order of the stage's first step. Steps of one stage run at the same time. Empty on proposals submitted before stages, whose every step is a stage of its own.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="review",
            name="coi_declaration_text",
            field=models.TextField(
                blank=True,
                help_text="The declaration the reviewer confirmed, as worded when they did; empty for the platform's standard one.",
            ),
        ),
        migrations.AddField(
            model_name="review",
            name="criteria_scores",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="The reviewer's score per criterion of the step the review is given in, 1-5, keyed by criterion UUID.",
            ),
        ),
        migrations.AddField(
            model_name="review",
            name="rejection_cause",
            field=models.CharField(
                blank=True,
                choices=[
                    ("declined", "Declined by the reviewer"),
                    ("expired", "Expired at its deadline"),
                    (
                        "withdrawn",
                        "Withdrawn when the reviewer joined the proposal team",
                    ),
                ],
                help_text="Why a rejected review was closed: declined by the reviewer, expired at its deadline, or withdrawn when the reviewer joined the proposal team. Empty on reviews rejected before this was recorded.",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="reviewerprofile",
            name="standing",
            field=models.CharField(
                choices=[
                    ("active", "Active"),
                    ("suspended", "Suspended"),
                    ("blocked", "Blocked"),
                ],
                db_index=True,
                default="active",
                help_text="Only an active reviewer is selected for, invited to or offered new review work.",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="reviewerprofile",
            name="standing_changed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="reviewerprofile",
            name="standing_changed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="reviewerprofile",
            name="standing_reason",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="workflowcriterion",
            name="weight",
            field=models.PositiveSmallIntegerField(
                default=1,
                help_text="The criterion's weight in a review's overall score, relative to the other criteria of the step.",
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(100),
                ],
            ),
        ),
        migrations.AlterField(
            model_name="callworkflowstep",
            name="checklist",
            field=models.ForeignKey(
                blank=True,
                help_text="Evaluation form for this step.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="checklist.checklist",
            ),
        ),
        migrations.AlterField(
            model_name="callworkflowstep",
            name="completer",
            field=models.CharField(
                blank=True,
                choices=[
                    ("call_manager", "Call manager"),
                    ("offering_manager", "Offering manager"),
                    ("reviewer", "Reviewer"),
                    ("panel_member", "Panel member"),
                    ("panel_chair", "Panel chair or vice-chair"),
                    ("applicant", "Applicant"),
                    ("proposal_role", "Evaluator role on the proposal"),
                ],
                help_text="Role expected to complete this step.",
                max_length=32,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="callworkflowstep",
            name="display_order",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="Deprecated: steps are ordered by order.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="callworkflowstep",
            name="key",
            field=models.SlugField(
                help_text="Identifier of the step within its call. Steps the call was seeded with keep the keys of the former step catalog.",
                max_length=64,
            ),
        ),
        migrations.AlterField(
            model_name="callworkflowstep",
            name="min_contributions",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Minimum contributions submitted before the step can complete. On a step without contributors, the minimum of the proposal's submitted reviews.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="callworkflowstep",
            name="min_score_threshold",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Minimum average score required before this step can complete with any outcome but declined (a completion gate; it does not auto-reject lower scores). A step with contributors scores its own submitted reviews. A step without contributors that reads input steps takes the score of the latest completed input step that has one: the average of its reviews, else of the rating answers in its checklists. Otherwise all the proposal's submitted reviews count.",
                max_digits=5,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="callworkflowstepnotificationrule",
            name="recipient",
            field=models.CharField(
                choices=[
                    ("applicant", "Applicant"),
                    ("responsible_role", "Responsible role"),
                    ("assigned_reviewers", "Assigned reviewers"),
                    ("call_managers", "Call managers"),
                    ("all_pool_reviewers", "All pool reviewers"),
                    ("panel_chair", "Panel chair and vice-chair"),
                    ("proposal_role", "Holders of an evaluator role"),
                    ("proposal_role_lead", "Lead of an evaluator role"),
                    ("offering_teams", "Teams of the proposal's offerings"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="callworkflowstepnotificationrule",
            name="trigger",
            field=models.CharField(
                choices=[
                    ("step_started", "Step started"),
                    ("step_completed", "Step completed"),
                    ("step_rejected", "Step rejected"),
                    ("step_expired", "Step expired"),
                    ("deadline_approaching", "Deadline approaching"),
                    ("changes_requested", "Changes requested from the applicant"),
                    ("changes_submitted", "Changes submitted by the applicant"),
                    ("contribution_reopened", "Contribution reopened"),
                    ("contribution_submitted", "Contribution submitted"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="proposalworkflowstepinstance",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("active", "Active"),
                    ("completed", "Completed"),
                    ("expired", "Expired"),
                    ("skipped", "Skipped"),
                    ("withdrawn", "Withdrawn"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="proposalworkflowstepinstance",
            name="step",
            field=models.CharField(
                help_text="Key of the call step, copied from it.", max_length=64
            ),
        ),
        migrations.AlterField(
            model_name="review",
            name="reviewer",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="review",
            name="summary_score",
            field=models.PositiveSmallIntegerField(
                blank=True,
                default=0,
                help_text="The review's overall score, 1-5 (0 until rated). When the step has criteria, it is their weighted average.",
                validators=[django.core.validators.MaxValueValidator(5)],
            ),
        ),
        migrations.AlterField(
            model_name="round",
            name="cutoff_time",
            field=models.DateTimeField(
                blank=True,
                help_text="When submission closes. Empty: the round stays open until a call manager closes it.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="proposal",
            name="panel",
            field=models.ForeignKey(
                blank=True,
                help_text="The call panel evaluating the proposal. Set from its science sub-domain when evaluation starts; changed by those managing the call and the completer of its panel assignment step. Never shown to the applicant team.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="proposals",
                to="proposal.callpanel",
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="assigns_roles",
            field=models.ManyToManyField(
                blank=True,
                help_text="Assignment step: the evaluator roles whose holders the completer picks in this step, such as a lead and a second rapporteur.",
                related_name="assigning_steps",
                to="proposal.callproposalrole",
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstep",
            name="completer_proposal_role",
            field=models.ForeignKey(
                blank=True,
                help_text="The evaluator role whose holders complete this step, when the completer is proposal_role.",
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="completed_steps",
                to="proposal.callproposalrole",
            ),
        ),
        migrations.AddField(
            model_name="callworkflowstepnotificationrule",
            name="proposal_role",
            field=models.ForeignKey(
                blank=True,
                help_text="Only for the proposal_role and proposal_role_lead audiences: the evaluator role whose holders, or whose lead, are notified.",
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="notification_rules",
                to="proposal.callproposalrole",
            ),
        ),
        migrations.AddConstraint(
            model_name="callworkflowstepnotificationrule",
            constraint=models.UniqueConstraint(
                condition=models.Q(("proposal_role__isnull", True)),
                fields=("workflow_step", "trigger", "recipient"),
                name="proposal_unique_step_notification_rule",
            ),
        ),
        migrations.AddConstraint(
            model_name="callworkflowstepnotificationrule",
            constraint=models.UniqueConstraint(
                condition=models.Q(("proposal_role__isnull", False)),
                fields=("workflow_step", "trigger", "recipient", "proposal_role"),
                name="proposal_unique_step_role_notification_rule",
            ),
        ),
        migrations.AddField(
            model_name="proposalworkflowstepinstance",
            name="run",
            field=models.ForeignKey(
                blank=True,
                help_text="The post-award run the step belongs to; empty for a step of the proposal's evaluation.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="step_instances",
                to="proposal.proposalrun",
            ),
        ),
        # Last, so that the two tables read outside the proposal module --
        # calls (for every user's visible organisations) and requested
        # offerings (marketplace filters) -- stay locked only for their own
        # operations, not for the whole migration.
        migrations.AddField(
            model_name="call",
            name="adoption_required",
            field=models.BooleanField(
                default=False,
                help_text="Whether a round's results must be adopted (by a board, say) before they are published. The adoption is then recorded while the round is deciding, and publication waits for it.",
            ),
        ),
        migrations.AddField(
            model_name="call",
            name="allowed_duration_months",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Durations, in months, the applicant chooses the project's length from. Empty when the applicant is offered no choice. Exclusive with fixed_duration_in_days.",
            ),
        ),
        migrations.AddField(
            model_name="call",
            name="applicant_cc_roles",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Names of the proposal roles (PROPOSAL.MANAGER for the principal investigator, PROPOSAL.ADMIN, ...) whose holders are copied (Cc) on every mail to the applicant about the proposal. Empty copies nobody.",
            ),
        ),
        migrations.AddField(
            model_name="call",
            name="budget_enforcement",
            field=models.CharField(
                choices=[
                    ("soft", "Soft: approving past the budget needs an override"),
                    ("hard", "Hard: an award past the budget is refused"),
                ],
                default="soft",
                help_text="How the allocation decision is held to the call's resource budgets. soft: approving an award past what remains needs an explicit override with a reason. hard: such an award is refused, and only staff may override it with a reason. Changing it affects only later decisions.",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="call",
            name="coi_declaration_text",
            field=models.TextField(
                blank=True,
                help_text="The conflict-of-interest declaration evaluators confirm, for every step asking them to that has no text of its own. Empty for the platform's standard declaration.",
            ),
        ),
        migrations.AddField(
            model_name="call",
            name="panel_vice_chair",
            field=models.ForeignKey(
                blank=True,
                help_text="Panel member who deputises for the panel chair. The same rules as the chair: must hold the panel member role on the call, is cleared when that role is revoked, and completes, contributes to and is notified for the steps of the panel chair.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="call",
            name="target_decision_days",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="Days from a round's cut-off to its decision the workflow is planned for, when the round has no allocation date. The workflow check compares the steps' durations with it.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="call",
            name="template_version",
            field=models.PositiveIntegerField(
                blank=True,
                editable=False,
                help_text="Version of the template the call was created from.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="requestedoffering",
            name="default_delegate",
            field=models.ForeignKey(
                blank=True,
                help_text="Who the provider hands its per-offering slots on to as they open, on steps whose slots may be handed within the offering's team. They accept or decline each, as for a slot handed on by hand.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="requestedoffering",
            name="rejection_reason",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Why the provider declined to take part in the call.",
            ),
        ),
        migrations.AlterField(
            model_name="requestedoffering",
            name="plan",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="marketplace.plan",
            ),
        ),
        migrations.AddField(
            model_name="call",
            name="template",
            field=models.ForeignKey(
                blank=True,
                help_text="The call template the call was created from.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="calls",
                to="proposal.calltemplate",
            ),
        ),
    ]
