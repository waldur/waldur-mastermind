// A proposal, rendered by proposal.proposal_pdf.render_proposal_pdf.
//
// Every value arrives as a string in the `data` JSON input, already
// formatted and translated in Python, and only the sections the reader may
// read are present. Strings are inserted as text, never evaluated, so markup
// characters in a proposal's text print literally.

#let data = json(bytes(sys.inputs.data))
#let muted = luma(100)
#let hairline = 0.4pt + luma(200)

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
#set text(font: "Source Sans 3", size: 10pt)
#set par(justify: false)
// A row stays on one page, unless it is taller than a page.
#set table.cell(breakable: false)

// Typst breaks lines only between words, so a word wider than its column --
// a URL, a hash -- runs off the page. Words longer than 30 characters get a
// zero-width space every 15, as the table export does.
#let breakable(value) = {
  value
    .split(" ")
    .map(word => {
      let characters = word.clusters()
      if characters.len() <= 30 { word } else {
        characters.chunks(15).map(part => part.join()).join("\u{200B}")
      }
    })
    .join(" ")
}

// A cell or entry is a list of lines.
#let lines(items) = items.map(breakable).join(linebreak())

#let heading-text(title) = {
  v(7mm)
  block(sticky: true, text(size: 13pt, weight: "bold", title))
  v(1mm)
}
#let note(body) = text(size: 9pt, fill: muted, body)

#let facts(items) = grid(
  columns: (42mm, 1fr),
  column-gutter: 8pt,
  row-gutter: 5pt,
  ..items.map(fact => (text(fill: muted, fact.label), breakable(fact.value))).flatten(),
)

// A width is "auto", a share of the page ("30%") or of what is left ("1fr").
// Without widths every column is auto and the last takes what is left, so a
// table spans the page and never runs past its edge.
#let width(value) = if value.ends-with("fr") {
  float(value.slice(0, -2)) * 1fr
} else if value.ends-with("%") {
  float(value.slice(0, -1)) * 1%
} else { auto }
#let data-table(columns, rows, widths) = table(
  columns: if widths == none {
    (auto,) * (columns.len() - 1) + (1fr,)
  } else { widths.map(width) },
  align: left + top,
  inset: (x: 5pt, y: 4pt),
  stroke: (_, y) => if y == 0 { (bottom: 0.8pt + black) } else { (bottom: hairline) },
  table.header(..columns.map(column => text(weight: "semibold", column))),
  ..rows.map(row => row.map(lines)).flatten(),
)

#let entries(items) = for entry in items {
  block(
    breakable: true,
    above: 4mm,
    below: 2mm,
    {
      text(weight: "semibold", entry.title)
      if entry.meta != "" {
        h(6pt)
        note(entry.meta)
      }
      for line in entry.lines {
        par(breakable(line))
      }
    },
  )
}

// Title -----------------------------------------------------------------------

#text(size: 20pt, weight: "bold", data.title)
#if data.subtitle != "" {
  v(1mm)
  note(data.subtitle)
}

// Sections --------------------------------------------------------------------

#for section in data.sections {
  heading-text(section.heading)
  let section-note = section.at("note", default: "")
  if section.kind != "empty" and section-note != "" {
    note(section-note)
    v(1mm)
  }
  if section.kind == "facts" {
    facts(section.facts)
  } else if section.kind == "text" {
    // Paragraphs as the applicant wrote them.
    for paragraph in section.body.split("\n") {
      par(breakable(paragraph))
    }
  } else if section.kind == "table" {
    data-table(section.columns, section.rows, section.at("widths", default: none))
  } else if section.kind == "list" {
    list(..section.items.map(breakable))
  } else if section.kind == "entries" {
    entries(section.entries)
  } else {
    note(section-note)
  }
}
