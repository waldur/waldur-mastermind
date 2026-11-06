// A call's workflow validation, rendered by
// proposal.workflow_report.create_workflow_report_pdf.
//
// Every value arrives as a string in the `report` JSON input, already
// formatted and translated in Python. Strings are inserted as text, never
// evaluated, so markup characters in step or call names print literally.

#let data = json(bytes(sys.inputs.report))
#let labels = data.labels
#let muted = luma(100)
#let hairline = 0.4pt + luma(200)
#let danger = rgb("#b42318")
#let warning = rgb("#b54708")
#let success = rgb("#067647")

#set document(title: data.title + " — " + data.call)
#set page(
  paper: "a4",
  margin: (x: 16mm, top: 18mm, bottom: 22mm),
  footer: context {
    set text(size: 8pt, fill: muted)
    data.call
    h(1fr)
    counter(page).display("1 / 1", both: true)
  },
)
#set text(font: "Source Sans 3", size: 9.5pt)
#set par(justify: false)

#let caption(body) = text(size: 8pt, weight: "semibold", fill: muted, upper(body))
#let section(title) = {
  v(8mm)
  text(size: 13pt, weight: "bold", title)
  v(2mm)
}
#let note(body) = text(size: 8.5pt, fill: muted, body)
#let level-colour(level) = if level == "error" { danger } else if level == "warning" { warning } else { muted }

#let grid-table(columns, header, rows) = table(
  columns: columns,
  align: left + top,
  inset: (x: 5pt, y: 4pt),
  stroke: (_, y) => if y == 0 { (bottom: 0.8pt + black) } else { (bottom: hairline) },
  table.header(..header.map(h => text(weight: "semibold", h))),
  ..rows.flatten(),
)

// Title -------------------------------------------------------------------

#caption(data.title)
#v(1mm)
#text(size: 20pt, weight: "bold", data.call)
#v(3mm)
#grid(
  columns: 2,
  column-gutter: 10pt,
  row-gutter: 4pt,
  ..data.facts.map(fact => (text(fill: muted, fact.label), fact.value)).flatten(),
)

// Workflow check ------------------------------------------------------------

#section(labels.check)
#data.verdict
#v(2mm)
#grid(
  columns: 3,
  column-gutter: 14pt,
  text(fill: danger, weight: "semibold", data.summary.error_label + ": " + data.summary.error),
  text(fill: warning, weight: "semibold", data.summary.warning_label + ": " + data.summary.warning),
  text(fill: muted, weight: "semibold", data.summary.info_label + ": " + data.summary.info),
)
#v(2mm)
#if data.findings.len() == 0 {
  note(labels.no_findings)
} else {
  grid-table(
    (1fr, 30%, auto),
    (labels.finding, labels.where, labels.severity),
    data.findings.map(f => (
      f.message,
      text(fill: muted, f.where),
      text(fill: level-colour(f.level), weight: "semibold", f.severity),
    )),
  )
}

// Steps -------------------------------------------------------------------

#section(labels.steps)
#grid-table(
  (auto, 1fr, auto, 22%, auto),
  ("#", labels.step, labels.kind, labels.completer, labels.duration),
  data.steps.map(s => (
    text(fill: muted, s.number),
    {
      text(weight: "semibold", fill: if s.enabled { black } else { muted }, s.name)
      if s.phase != "" { h(4pt); text(size: 8pt, fill: muted, "· " + s.phase) }
      if s.description != "" { linebreak(); text(size: 8.5pt, s.description) }
      if s.contributors != "" {
        linebreak()
        text(size: 8.5pt, fill: muted, labels.contributors + ": " + s.contributors)
      }
      if s.outcomes != "" {
        linebreak()
        text(size: 8.5pt, fill: muted, labels.outcomes + ": " + s.outcomes)
      }
      if s.notes != "" { linebreak(); text(size: 8.5pt, fill: muted, s.notes) }
    },
    s.kind,
    s.completer,
    s.duration,
  )),
)

// Matrices, landscape: a column per role ------------------------------------

#set page(flipped: true)

#text(size: 13pt, weight: "bold", labels.roles)
#v(1mm)
#note(labels.roles_intro + " " + data.role_matrix.legend.join(" · "))
#v(2mm)
#let role-columns = data.role_matrix.columns
#grid-table(
  (22%, ..role-columns.map(_ => 1fr)),
  (labels.step, ..role-columns),
  data.role_matrix.rows.map(row => (
    text(fill: if row.enabled { black } else { muted }, row.step),
    ..row.cells.map(cell => text(font: "Source Sans 3", weight: "semibold", cell)),
  )),
)

#section(labels.notifications)
#note(labels.notifications_intro)
#v(2mm)
#let notification-columns = data.notification_matrix.columns
#grid-table(
  (18%, ..notification-columns.map(_ => 1fr)),
  (labels.step, ..notification-columns),
  data.notification_matrix.rows.map(row => (
    text(fill: if row.enabled { black } else { muted }, row.step),
    ..row.cells.map(cell => text(size: 8pt, cell.join("\n"))),
  )),
)

#if data.flags.len() > 0 {
  v(4mm)
  text(weight: "semibold", labels.flags)
  v(1mm)
  grid-table(
    (1fr, 25%, auto),
    (labels.finding, labels.where, labels.severity),
    data.flags.map(f => (f.message, text(fill: muted, f.where), f.severity)),
  )
}

#if data.always_sent.len() > 0 {
  v(4mm)
  text(weight: "semibold", labels.always_sent)
  v(1mm)
  grid-table(
    (1fr, 1fr),
    (labels.event, labels.audience),
    data.always_sent.map(a => (a.event, a.audience)),
  )
}

// Scenarios ------------------------------------------------------------------

#set page(flipped: false)

#text(size: 13pt, weight: "bold", labels.scenarios)
#v(2mm)
#if data.scenarios.len() == 0 {
  note(labels.no_scenarios)
} else {
  note(data.scenarios_checked)
  v(2mm)
  grid-table(
    (1fr, 1fr, auto),
    (labels.scenario, labels.detail, labels.status),
    data.scenarios.map(s => (
      s.name,
      text(fill: muted, s.detail),
      text(fill: if s.passed { success } else { danger }, weight: "semibold", s.status),
    )),
  )
}
