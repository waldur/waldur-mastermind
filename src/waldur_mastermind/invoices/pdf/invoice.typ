// Invoice PDF, rendered by invoices.utils.create_invoice_pdf.
//
// Every value arrives as a string in the `invoice` JSON input, already
// formatted and translated in Python. Strings are inserted as text, never
// evaluated, so markup characters in customer or item names print literally.

#let data = json(bytes(sys.inputs.invoice))
#let labels = data.labels
#let muted = luma(100)
#let hairline = 0.4pt + luma(200)

#set document(title: data.title)
#set page(
  paper: "a4",
  margin: (x: 20mm, top: 20mm, bottom: 25mm),
  footer: context {
    set text(size: 8pt, fill: muted)
    data.title
    h(1fr)
    counter(page).display("1 / 1", both: true)
  },
)
#set text(font: "Source Sans 3", size: 10pt)

#let caption(body) = text(size: 8pt, weight: "semibold", fill: muted, upper(body))

#let party(title, lines) = block({
  caption(title)
  v(4pt)
  strong(lines.first())
  for line in lines.slice(1) {
    linebreak()
    line
  }
})

#grid(
  columns: (1fr, auto),
  align: (left + bottom, left + bottom),
  text(size: 20pt, weight: "bold", data.title),
  grid(
    columns: 2,
    column-gutter: 10pt,
    row-gutter: 5pt,
    ..data.facts.map(fact => (text(fill: muted, fact.label), fact.value)).flatten(),
  ),
)

#v(10mm)

#grid(
  columns: (1fr, 1fr),
  column-gutter: 10mm,
  party(labels.issuer, data.issuer),
  party(labels.customer, data.customer),
)

#v(10mm)

#table(
  columns: (1fr, auto, auto, auto),
  align: (left, right, right, right),
  inset: (x: 6pt, y: 5pt),
  stroke: (_, y) => if y == 0 { (bottom: 0.8pt + black) } else { (bottom: hairline) },
  table.header(
    ..(labels.item, labels.quantity, labels.unit_price, labels.price).map(
      heading => text(weight: "semibold", heading),
    ),
  ),
  ..for project in data.projects {
    (table.cell(colspan: 4, inset: (x: 6pt, top: 12pt, bottom: 5pt), strong(project.name)),)
    for item in project.items {
      (
        {
          item.name
          if item.period != "" {
            linebreak()
            text(size: 8pt, fill: muted, item.period)
          }
        },
        item.quantity,
        item.unit_price,
        item.price,
      )
    }
  },
)

#v(6mm)

#align(right, grid(
  columns: 2,
  column-gutter: 16pt,
  row-gutter: 6pt,
  align: (left, right),
  ..data.totals.map(row => {
    if row.emphasis { (strong(row.label), strong(row.value)) } else { (row.label, row.value) }
  }).flatten(),
))
