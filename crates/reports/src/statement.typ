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

// The letterhead: the society's logo at the top left with its name beside
// it, and the statement's date and period on the right.
#grid(
  columns: (auto, 1fr, auto),
  column-gutter: 14pt,
  align: (left + horizon, left + horizon, right + horizon),
  if data.logo != none { image(data.logo, height: 58pt) } else [],
  {
    if data.org != "" {
      text(size: 16pt, weight: "bold", fill: rgb("#0f172a"))[#data.org]
      linebreak()
    }
    text(size: 12pt, weight: "bold", fill: accent, tracking: 0.6pt)[#upper(data.title)]
  },
  {
    text(size: 7pt, fill: grey, tracking: 0.4pt)[STATEMENT DATE]
    linebreak()
    text(size: 9pt, weight: "bold")[#data.statement_date]
    linebreak()
    text(size: 7pt, fill: grey, tracking: 0.4pt)[PERIOD]
    linebreak()
    text(size: 9pt, weight: "bold")[#data.period]
  },
)
#v(4pt)
#line(length: 100%, stroke: 2.5pt + accent)
#v(4pt)

#block(fill: rgb("#f5f5f5"), inset: 7pt, radius: 4pt, width: 100%)[
  #grid(
    columns: (1fr, 1fr),
    [*Member:* #data.name #if data.pf_no != "" [#h(10pt) *PF No:* #data.pf_no] \ *Contact:* #data.phone],
    align(right)[
      *Account Status:* #text(fill: if data.active { green } else { red }, weight: "bold")[#data.status]
    ],
  )
]
#v(6pt)

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
        (table.cell(colspan: n, fill: rgb("#f0f0f0"), text(fill: rgb("#888888"), style: "italic")[#r.note]),)
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
    .map(r => r.cells.enumerate().map(((i, v)) => {
      let body = carried(r, cell(v, data.savings_numeric.at(i)))
      if r.withdrawal and data.savings_columns.at(i) == "Amount" { text(fill: red, body) } else { body }
    }))
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
