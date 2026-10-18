// A member's statement: header, client details, the loans beside the
// savings, and the closing figures. Everything shown arrives formatted in
// data.json, so nothing here is parsed as markup.
#let data = json("data.json")
#let accent = rgb("#2b5797")
#let green = rgb("#28a745")
#let red = rgb("#dc3545")
#let grey = rgb("#666666")

#set document(title: data.title + " — " + data.name)
#set page(
  paper: "a4",
  flipped: true,
  margin: (x: 12mm, top: 12mm, bottom: 14mm),
  footer: context text(size: 7pt, fill: rgb("#999999"))[
    #if data.footer != "" [#data.footer | ]Statement generated on #data.generated | Period: #data.period
    #h(1fr) Page #counter(page).display("1 of 1", both: true)
  ],
)
#set text(font: "Liberation Sans", size: 8pt)

// The letterhead as 1.x printed it: the logo at the top left; the society,
// the title, the member and the period centred over a rule; then the
// member's details in a grey box. Sizes are 1.x's, in points. Each line of a
// stack is a cap height tall, so the gaps between them stand for 1.x's line
// spacing as well as its margins.
#block(width: 100%, inset: (bottom: 9pt), stroke: (bottom: 2.25pt + accent), below: 11pt, {
  if data.logo != none { place(top + left, image(data.logo, height: 37.5pt)) }
  let lines = ()
  if data.org != "" {
    lines += (text(size: 11.25pt, weight: "bold", fill: rgb("#0f172a"), tracking: 0.4pt, data.org), 5.5pt)
  }
  lines += (
    text(size: 16.5pt, weight: "bold", fill: accent, data.title),
    10pt,
    text(size: 12pt, fill: rgb("#333333"), data.name),
    8pt,
    text(size: 8.25pt, fill: grey, "For the period: " + data.period),
  )
  align(center, stack(..lines))
})

#block(fill: rgb("#f5f5f5"), inset: 6pt, radius: 3.75pt, width: 100%, below: 11pt, grid(
  columns: (1fr, 1fr),
  align: (left, right),
  row-gutter: 4pt,
  [*Client Name:* #data.name], [*Statement Date:* #data.statement_date],
  [*Contact:* #data.phone],
  [*Account Status:* #text(fill: if data.active { green } else { red }, weight: "bold", data.status)],
  ..if data.pf_no != "" { ([*PF No:* #data.pf_no], []) } else { () },
))

#let band(body, fill) = block(fill: fill, inset: 5pt, width: 100%, below: 0pt, text(fill: white, weight: "bold", size: 9pt, body))
#let nothing(what) = block(inset: 8pt, text(fill: grey)[No #what transactions in this period])
#let cell(v, numeric) = if numeric { align(right)[#v] } else [#v]
// A balance carried into the period reads apart from the period's entries.
#let carried(r, body) = if r.at("bf", default: false) { text(style: "italic", fill: grey, body) } else { body }
// Notes (else Type) takes the spare width, so each table fills its column.
#let widths(columns) = {
  let at = columns.position(c => c == "Notes")
  if at == none { at = columns.position(c => c == "Type") }
  range(columns.len()).map(i => if i == at { 1fr } else { auto })
}

// A greyed note across a table: a suspension.
#let note_row(n, body) = (table.cell(colspan: n, fill: rgb("#f0f0f0"), text(fill: rgb("#888888"), style: "italic", body)),)

#let loan_table(loan) = {
  let n = data.loan_columns.len()
  table(
    columns: widths(data.loan_columns),
    stroke: 0.5pt + rgb("#dddddd"),
    inset: (x: 3pt, y: 2.5pt),
    table.header(
      table.cell(colspan: n, fill: accent, text(fill: white, weight: "bold")[Loan: #loan.ref]),
      ..data.loan_columns.map(c => table.cell(fill: rgb("#e0e0e0"), text(size: 7pt, weight: "bold")[#c])),
    ),
    ..loan
      .rows
      .map(r => if "note" in r {
        note_row(n, r.note)
      } else {
        r.cells.enumerate().map(((i, v)) => carried(r, cell(v, data.loan_numeric.at(i))))
      })
      .flatten(),
  )
}

#let savings_table = table(
  columns: widths(data.savings_columns),
  stroke: 0.5pt + rgb("#dddddd"),
  inset: (x: 3pt, y: 2.5pt),
  table.header(..data.savings_columns.map(c => table.cell(fill: rgb("#e0e0e0"), text(size: 7pt, weight: "bold")[#c]))),
  ..data
    .savings
    .map(r => if "note" in r { note_row(data.savings_columns.len(), r.note) } else {
      r.cells.enumerate().map(((i, v)) => {
        let body = carried(r, cell(v, data.savings_numeric.at(i)))
        if r.withdrawal and data.savings_columns.at(i) == "Amount" { text(fill: red, body) } else { body }
      })
    })
    .flatten(),
)

#let loans_part = {
  band([LOANS], accent)
  if data.loans.len() == 0 { nothing("loan") } else {
    for loan in data.loans {
      loan_table(loan)
      v(6pt)
    }
  }
}
#let savings_part = {
  band([SAVINGS / SHARES], green)
  if data.savings.len() == 0 { nothing("savings") } else { savings_table }
}

#if data.show_loans and data.show_savings {
  grid(columns: (1fr, 0.6fr), column-gutter: 14pt, loans_part, savings_part)
} else if data.show_loans {
  loans_part
} else if data.show_savings {
  align(center, block(width: 70%, savings_part))
}

#v(8pt)
#block(fill: rgb("#f9f9f9"), inset: 8pt, radius: 4pt, width: 100%)[
  #grid(
    columns: (1fr, 1fr),
    column-gutter: 14pt,
    if data.show_loans [
      *Total Net Outstanding:* #data.net \
      #text(size: 7pt, fill: grey)[(Principal + Accrued Interest)] \
      *Total Gross Outstanding:* #data.gross \
      #text(size: 7pt, fill: grey)[(Principal + Total Expected Interest)]
    ] else [],
    if data.show_savings [*Savings Balance:* #data.savings_balance] else [],
  )
]
