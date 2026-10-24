// Table or chart export, rendered by core.views.ExportPdfView.
//
// The client has assembled the title, columns and rows. They arrive as
// strings in the `data` JSON input and are inserted as text, never evaluated,
// so markup characters in cell values print literally. A chart arrives as
// chart.png next to this file.

#let data = json(bytes(sys.inputs.data))
#let muted = luma(100)
#let hairline = 0.4pt + luma(200)
#let columns = data.at("columns", default: ())

#set document(title: data.title)
#set page(
  paper: "a4",
  // Wide tables get the long edge.
  flipped: columns.len() > 6,
  margin: (x: 15mm, top: 15mm, bottom: 20mm),
  footer: context {
    set text(size: 8pt, fill: muted)
    data.title
    h(1fr)
    counter(page).display("1 / 1", both: true)
  },
)

// The more columns, the smaller the text, so each keeps a readable width.
#set text(
  font: "Source Sans 3",
  size: if columns.len() <= 8 { 9pt } else if columns.len() <= 12 { 8pt } else if columns.len() <= 16 { 7pt } else { 6pt },
)

// Typst breaks lines only between words, so a word wider than its column
// runs into the next one. The more columns, the narrower each is, so words
// longer than `limit` characters get a zero-width space every `chunk`. It
// also shows up in text copied from them: a readable table is worth more
// than a clean copy of a UUID.
#let (limit, chunk) = if columns.len() <= 8 { (20, 8) } else if columns.len() <= 16 {
  (12, 6)
} else if columns.len() <= 30 { (8, 4) } else { (4, 2) }
// str.len() counts bytes: cheap, and never below the number of characters.
#let cell(value) = if value.len() <= limit { value } else {
  value
    .split(" ")
    .map(word => {
      let characters = word.clusters()
      if characters.len() <= limit { word } else {
        characters.chunks(chunk).map(part => part.join()).join("\u{200B}")
      }
    })
    .join(" ")
}

#if data.title != "" {
  text(size: 16pt, weight: "bold", data.title)
  v(4mm)
}

#if data.at("image", default: false) {
  // The chart fills the width, unless that makes it taller than the rest of
  // the page: then it fits the height, so it never leaves the title alone.
  block(height: 1fr, width: 100%, layout(size => {
    let chart = image("chart.png", width: size.width)
    if measure(chart).height > size.height {
      image("chart.png", height: size.height)
    } else {
      chart
    }
  }))
} else {
  table(
    columns: columns.len(),
    inset: (x: if columns.len() > 16 { 2pt } else { 4pt }, y: 3pt),
    stroke: (_, y) => if y == 0 { (bottom: 0.8pt + black) } else { (bottom: hairline) },
    // The header row repeats at the top of every page.
    table.header(..columns.map(heading => text(weight: "semibold", cell(heading)))),
    ..data.rows.flatten().map(cell),
  )
}
