// A report: the letterhead, then one table. The figures arrive already
// formatted in data.json, so nothing here is parsed as markup.
#let data = json("data.json")
#let accent = rgb("#2563eb")
#let ink = rgb("#0f172a")
#let muted = rgb("#64748b")

#set document(title: data.title)
#set page(
  paper: "a4",
  flipped: true,
  margin: (x: 10mm, top: 10mm, bottom: 14mm),
  footer: context text(size: 7pt, fill: rgb("#94a3b8"))[
    Generated on #data.generated #h(1fr) Page #counter(page).display("1 of 1", both: true)
  ],
)
#set text(font: "Liberation Sans", size: if data.columns.len() > 12 { 7pt } else { 8pt }, fill: rgb("#1f2937"))

#let words = {
  if data.org != "" {
    text(size: 15pt, weight: "bold", fill: ink)[#data.org]
    linebreak()
  }
  text(size: 11pt, weight: "bold", fill: accent)[#upper(data.title)]
  if data.subtitle != "" {
    linebreak()
    text(size: 8.5pt, fill: muted)[#data.subtitle]
  }
}
#if data.logo != none {
  grid(columns: (auto, 1fr), column-gutter: 10pt, align: horizon, image(data.logo, height: 40pt), words)
} else {
  words
}
#v(2pt)
#line(length: 100%, stroke: 2pt + accent)
#v(6pt)

#let shown(i, v) = if data.numeric.at(i) { align(right)[#v] } else [#v]
// The name column takes the page's spare width, so the table fills it.
#let wide = {
  let at = data.columns.position(c => c == "Name" or c == "Member")
  if at == none { data.numeric.position(n => not n) } else { at }
}
#table(
  columns: range(data.columns.len()).map(i => if i == wide { 1fr } else { auto }),
  stroke: 0.5pt + rgb("#e2e8f0"),
  inset: (x: 4pt, y: 3pt),
  table.header(..data.columns.map(c => table.cell(fill: rgb("#f1f5f9"), text(weight: "bold", fill: ink)[#c]))),
  ..data.rows.map(row => row.enumerate().map(((i, v)) => shown(i, v))).flatten(),
  ..if data.total != none {
    data.total.enumerate().map(((i, v)) => table.cell(fill: rgb("#f8fafc"), text(weight: "bold", shown(i, v))))
  } else { () },
)
