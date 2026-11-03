// Job performance report, rendered by waldur_firecrest.pdf.job_report_pdf.
//
// Everything arrives in the `report` JSON input already formatted in
// Python: texts as strings (inserted as text, never evaluated) and chart
// lines as points scaled to 0..1 on both axes, so this file only lays out.
// It reads nothing outside its directory and imports no packages.

#let data = json(bytes(sys.inputs.report))
#let muted = luma(100)
#let hairline = 0.4pt + luma(200)
#let gridline = 0.3pt + luma(228)
#let plot-w = 148mm
#let plot-h = 40mm
#let axis-w = 14mm

#set document(title: data.title)
#set page(
  paper: "a4",
  margin: (x: 18mm, top: 18mm, bottom: 22mm),
  footer: context {
    set text(size: 8pt, fill: muted)
    data.footer
    h(1fr)
    counter(page).display("1 / 1", both: true)
  },
)
#set text(font: "Source Sans 3", size: 9.5pt)

#let caption(body) = text(size: 7.5pt, weight: "semibold", fill: muted, upper(body))

#let tile(t) = box(
  width: 100%,
  inset: 8pt,
  radius: 4pt,
  stroke: if t.flag { 0.8pt + rgb("#d97706") } else { hairline },
  {
    caption(t.label)
    v(3pt)
    text(size: 15pt, weight: "bold", t.value)
    if t.detail != "" {
      linebreak()
      text(size: 8pt, fill: muted, t.detail)
    }
  },
)

#let at(p) = (p.at(0) * plot-w, (1 - p.at(1)) * plot-h)

#let plot(c) = block(breakable: false, width: 100%, above: 10pt, {
  text(weight: "semibold", c.title)
  if c.unit != "" { text(fill: muted, " (" + c.unit + ")") }
  if c.subtitle != "" {
    linebreak()
    text(size: 8pt, fill: muted, c.subtitle)
  }
  v(4pt)
  grid(
    columns: (axis-w, plot-w),
    column-gutter: 2mm,
    row-gutter: 2pt,
    box(width: axis-w, height: plot-h, {
      for t in c.y_ticks {
        place(top + right, dy: (1 - t.pos) * plot-h - 4pt, text(size: 7pt, fill: muted, t.label))
      }
    }),
    box(width: plot-w, height: plot-h, stroke: (left: hairline, bottom: hairline), {
      for t in c.y_ticks {
        place(top + left, dy: (1 - t.pos) * plot-h, line(length: plot-w, stroke: gridline))
      }
      for l in c.lines {
        for segment in l.segments {
          if segment.len() > 1 {
            place(top + left, curve(
              stroke: (
                paint: rgb(l.color),
                thickness: l.width * 1pt,
                dash: if l.dashed { "dashed" } else { none },
                join: "round",
              ),
              curve.move(at(segment.first())),
              ..segment.slice(1).map(p => curve.line(at(p))),
            ))
          }
        }
      }
      if c.empty != "" {
        place(center + horizon, text(size: 8pt, fill: muted, c.empty))
      }
    }),
    [],
    box(width: plot-w, height: 9pt, {
      for t in c.x_ticks {
        place(top + left, dx: t.pos * plot-w - 12mm, box(width: 24mm, align(center, text(size: 7pt, fill: muted, t.label))))
      }
    }),
  )
  if c.legend.len() > 0 {
    v(2pt)
    for item in c.legend {
      box(baseline: -2pt, line(length: 10pt, stroke: 1.5pt + rgb(item.color)))
      h(3pt)
      text(size: 7.5pt, fill: muted, item.label)
      h(10pt)
    }
  }
})

// Heading

#grid(
  columns: (1fr, auto),
  align: (left + bottom, right + bottom),
  {
    text(size: 18pt, weight: "bold", data.title)
    if data.subtitle != "" {
      linebreak()
      text(fill: muted, data.subtitle)
    }
  },
  text(size: 8pt, fill: muted, data.generated),
)

#v(6mm)

#if data.notice != "" {
  block(width: 100%, inset: 8pt, radius: 4pt, fill: luma(245), stroke: hairline, data.notice)
  v(4mm)
}

#if data.tiles.len() > 0 {
  grid(columns: (1fr, 1fr, 1fr), column-gutter: 6pt, row-gutter: 6pt, ..data.tiles.map(tile))
  v(4mm)
}

#for w in data.warnings {
  block(width: 100%, inset: 6pt, radius: 3pt, stroke: 0.6pt + rgb("#d97706"), text(size: 8.5pt, w))
}

#for c in data.charts { plot(c) }

// Job

#block(breakable: false, above: 8mm, {
  text(size: 12pt, weight: "bold", data.labels.job)
  v(3pt)
  grid(
    columns: (40mm, 1fr),
    row-gutter: 5pt,
    ..data.facts.map(f => (text(fill: muted, f.label), f.value)).flatten(),
  )
})

#if data.processes.rows.len() > 0 {
  block(above: 8mm, {
    text(size: 12pt, weight: "bold", data.labels.processes)
    if data.processes.note != "" {
      linebreak()
      text(size: 8pt, fill: muted, data.processes.note)
    }
    v(3pt)
    table(
      columns: (1fr, auto, auto, auto, auto, auto),
      align: (left, right, right, right, right, right),
      inset: (x: 5pt, y: 4pt),
      stroke: (_, y) => if y == 0 { (bottom: 0.8pt + black) } else { (bottom: hairline) },
      table.header(..data.processes.headers.map(h => text(weight: "semibold", h))),
      ..data.processes.rows.flatten(),
    )
  })
}
