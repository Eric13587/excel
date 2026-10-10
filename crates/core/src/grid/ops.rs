//! What a grid cell can say, beyond a bare figure.
//!
//! A cell is a list of **entries**, separated by commas or semicolons:
//!
//! ```text
//! 2,000                    the tab's default — a deposit, a contribution, a deduction
//! @W=500                   a named operation and its figure
//! 2,000, @W=500            both, in the same month
//! +@W=500                  added to whatever the month already holds
//! ```
//!
//! * An operation is `@` and a word; any unambiguous start of it will do, in
//!   any case: `@w`, `@W` and `@withdraw` are the same.
//! * `=` is optional, and so are thousands separators. A comma separates
//!   entries only when a space or an `@` follows it, so `2,000` stays two
//!   thousand; a figure whose commas are not thousands (`1,00`) is refused.
//! * Some operations take a second figure after a slash (`@T=20,000/12`), some
//!   none (`@B`). A zero entry is kept, so `@W=0` can remove a withdrawal.
//!
//! A cell holding anything other than one default entry is written with every
//! entry prefixed (`@D=2,000, @W=500`), so it reads back exactly.

use std::sync::LazyLock;

use regex::Regex;

use crate::money::{figure, round2};

/// What an operation's figure is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Figure {
    Money,
    Months,
}

/// One thing a cell can ask for.
#[derive(Debug, PartialEq)]
pub struct Op {
    /// Shown in the cell: the `D` in `@D=2,000`.
    pub code: &'static str,
    pub name: &'static str,
    /// Accepted after `@`, matched by prefix.
    pub words: &'static [&'static str],
    pub help: &'static str,
    /// The figure, or `None` for an operation that takes none (`@B`).
    pub value: Option<Figure>,
    /// A second figure after `/` — the months of a top-up.
    pub arg: Option<Figure>,
}

impl Op {
    fn matches(&self, word: &str) -> bool {
        let word = word.to_lowercase().replace('-', "");
        self.words.iter().any(|w| w.starts_with(&word))
    }

    /// How the help panel writes this operation.
    pub fn syntax(&self) -> String {
        match self.value {
            None => format!("@{}", self.code),
            Some(value) => {
                let mut figure = if value == Figure::Money { "amount" } else { "months" }.to_string();
                if self.arg.is_some() {
                    figure.push_str("/months");
                }
                format!("@{}={figure}", self.code)
            }
        }
    }
}

/// One thing a cell says: an operation and its figure(s).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Entry {
    pub op: &'static Op,
    /// Money, or a count of months for a months operation.
    pub amount: f64,
    pub arg: Option<i64>,
}

impl Entry {
    pub fn new(op: &'static Op, amount: f64) -> Entry {
        Entry { op, amount, arg: None }
    }
}

/// The operations one grid understands.
#[derive(Debug)]
pub struct OpSet {
    pub ops: Vec<&'static Op>,
    pub default: &'static Op,
    /// What a bare negative figure means, if anything (`-500` → withdrawal).
    pub negative: Option<&'static Op>,
}

static NUMBER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?:[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|[+-]?\.\d+)$").unwrap());
static LOOSE_NUMBER: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[+-]?[\d,]+(?:\.\d+)?$").unwrap());
static OPERATION: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^@\s*([A-Za-z][A-Za-z\-]*)\s*[=:]?\s*(.*)$").unwrap());
static MONTH_SUFFIX: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)\s*(m|mo|months?)$").unwrap());
static SLASH: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s*/\s*").unwrap());

fn money(text: &str, what: &str) -> Result<f64, String> {
    let compact = text.replace(' ', "");
    if !NUMBER.is_match(&compact) {
        if LOOSE_NUMBER.is_match(&compact) {
            return Err(format!(
                "{text:?} has commas in the wrong places{what} — write 2,000 or 2000, and separate \
                 entries with \", \"."
            ));
        }
        return Err(format!("{text:?} is not a number{what}."));
    }
    compact.replace(',', "").parse::<f64>().map(round2).map_err(|_| format!("{text:?} is not a number{what}."))
}

fn months(text: &str, what: &str) -> Result<i64, String> {
    let cleaned = MONTH_SUFFIX.replace(text.trim(), "").to_string();
    if cleaned.is_empty() || !cleaned.chars().all(|c| c.is_ascii_digit()) {
        return Err(format!("{text:?} is not a whole number of months{what}."));
    }
    cleaned.parse().map_err(|_| format!("{text:?} is not a whole number of months{what}."))
}

/// Does `rest` start (after optional `+` and spaces) with an `@`?
fn starts_an_operation(rest: &str) -> bool {
    let rest = rest.trim_start();
    let rest = rest.strip_prefix('+').map(str::trim_start).unwrap_or(rest);
    rest.starts_with('@')
}

/// Split a cell into entries: at `;`, at a comma followed by a space or an
/// operation, and at spaces that start an operation. Never inside `2,000`.
fn split_entries(text: &str) -> Vec<String> {
    let mut parts = Vec::new();
    let mut current = String::new();
    let chars: Vec<(usize, char)> = text.char_indices().collect();
    let mut i = 0;
    while i < chars.len() {
        let (at, ch) = chars[i];
        let rest = &text[at + ch.len_utf8()..];
        let boundary = match ch {
            ';' => true,
            ',' => rest.starts_with(char::is_whitespace) || starts_an_operation(rest),
            c if c.is_whitespace() => {
                // A run of spaces splits only if an operation follows it.
                let after = text[at..].trim_start();
                after.starts_with('@') || (after.starts_with('+') && starts_an_operation(after))
            }
            _ => false,
        };
        if boundary {
            parts.push(std::mem::take(&mut current));
            // Swallow the whitespace around the boundary.
            while i + 1 < chars.len() && chars[i + 1].1.is_whitespace() {
                i += 1;
            }
        } else {
            current.push(ch);
        }
        i += 1;
    }
    parts.push(current);
    parts.into_iter().map(|p| p.trim().to_string()).collect()
}

impl OpSet {
    pub fn new(ops: Vec<&'static Op>, default: &'static Op, negative: Option<&'static Op>) -> OpSet {
        OpSet { ops, default, negative }
    }

    pub fn resolve(&self, word: &str) -> Result<&'static Op, String> {
        let found: Vec<&'static Op> = self.ops.iter().copied().filter(|op| op.matches(word)).collect();
        let lower = word.to_lowercase();
        if let Some(exact) =
            found.iter().find(|op| op.code.eq_ignore_ascii_case(&lower) || op.words.contains(&lower.as_str()))
        {
            return Ok(exact);
        }
        match found.as_slice() {
            [one] => Ok(one),
            [] => Err(format!(
                "@{word} is not an operation here. This tab knows {}.",
                self.ops.iter().map(|op| format!("@{} ({})", op.code, op.name.to_lowercase())).collect::<Vec<_>>().join(", ")
            )),
            many => Err(format!(
                "@{word} could mean {} — type more of the word.",
                many.iter().map(|op| op.name.to_lowercase()).collect::<Vec<_>>().join(" or ")
            )),
        }
    }

    /// Read a cell's text into `(entries, append)`. `append` is true when the
    /// text began with `+@`: add to the month rather than replace it.
    pub fn parse(&self, text: &str) -> Result<(Vec<Entry>, bool), String> {
        let mut text = text.trim().replace(['—', '–'], "");
        text = text.trim().to_string();
        let append = {
            let t = text.trim_start();
            t.starts_with('+') && t.trim_start_matches('+').trim_start().starts_with('@')
        };
        if append {
            text = text.trim_start_matches('+').trim().to_string();
        }
        if text.is_empty() || text == "-" {
            return Ok((Vec::new(), append));
        }
        let mut entries = Vec::new();
        for part in split_entries(&text) {
            let part = part.strip_prefix('+').map(str::trim).unwrap_or(&part).to_string();
            if part.is_empty() {
                continue;
            }
            entries.push(self.parse_one(&part)?);
        }
        Ok((entries, append))
    }

    fn parse_one(&self, part: &str) -> Result<Entry, String> {
        if !part.starts_with('@') {
            let amount = money(part, "")?;
            if amount < 0.0 {
                return match self.negative {
                    Some(op) => Ok(Entry::new(op, -amount)),
                    None => Err(format!(
                        "A {} must be more than zero — clear the cell to remove it.",
                        self.default.name.to_lowercase()
                    )),
                };
            }
            return Ok(Entry::new(self.default, amount));
        }
        let captures =
            OPERATION.captures(part).ok_or_else(|| format!("{part:?} is not an operation — write it like @W=500."))?;
        let op = self.resolve(&captures[1])?;
        let mut rest = captures[2].trim().to_string();
        let what = format!(" for @{}", op.code);

        let Some(value) = op.value else {
            if !rest.is_empty() {
                return Err(format!(
                    "@{} ({}) takes no figure — type @{} on its own.",
                    op.code,
                    op.name.to_lowercase(),
                    op.code
                ));
            }
            return Ok(Entry::new(op, 0.0));
        };
        if rest.is_empty() {
            return Err(format!("@{} needs a figure, like {}.", op.code, op.syntax()));
        }
        let mut arg = None;
        if op.arg.is_some() {
            let pieces: Vec<String> = SLASH.split(&rest).filter(|p| !p.is_empty()).map(str::to_string).collect();
            if pieces.len() != 2 {
                return Err(format!("@{} needs the months too, like @{}=20,000/12.", op.code, op.code));
            }
            let m = months(&pieces[1], &what)?;
            if m <= 0 {
                return Err(format!("@{}: the months must be more than zero.", op.code));
            }
            rest = pieces[0].clone();
            arg = Some(m);
        }
        let amount = match value {
            Figure::Money => money(&rest, &what)?,
            Figure::Months => months(&rest, &what)? as f64,
        };
        if amount < 0.0 {
            return Err(format!(
                "@{} takes a positive figure; the operation already says which way the money moves.",
                op.code
            ));
        }
        Ok(Entry { op, amount, arg })
    }

    /// Write entries back as cell text — the inverse of [`OpSet::parse`].
    pub fn format(&self, entries: &[Entry]) -> String {
        if entries.is_empty() {
            return String::new();
        }
        if entries.len() == 1 && std::ptr::eq(entries[0].op, self.default) {
            return figure(entries[0].amount);
        }
        entries
            .iter()
            .map(|entry| match entry.op.value {
                None => format!("@{}", entry.op.code),
                Some(value) => {
                    let mut text = if value == Figure::Money {
                        figure(entry.amount)
                    } else {
                        (entry.amount as i64).to_string()
                    };
                    if let (Some(_), Some(arg)) = (entry.op.arg, entry.arg) {
                        text.push_str(&format!("/{arg}"));
                    }
                    format!("@{}={text}", entry.op.code)
                }
            })
            .collect::<Vec<_>>()
            .join(", ")
    }

    /// `(syntax, meaning)` for each operation, the default first.
    pub fn help_rows(&self) -> Vec<(String, &'static str)> {
        let mut rows = vec![(format!("{}  or  {}", figure(2000.0), self.default.syntax()), self.default.help)];
        rows.extend(
            self.ops.iter().filter(|op| !std::ptr::eq(**op, self.default)).map(|op| (op.syntax(), op.help)),
        );
        rows
    }

    pub fn is_default(&self, op: &'static Op) -> bool {
        std::ptr::eq(op, self.default)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    static DEPOSIT: Op = Op {
        code: "D",
        name: "Deposit",
        words: &["d", "deposit", "i", "increment"],
        help: "",
        value: Some(Figure::Money),
        arg: None,
    };
    static WITHDRAW: Op = Op {
        code: "W",
        name: "Withdrawal",
        words: &["w", "withdrawal", "withdraw"],
        help: "",
        value: Some(Figure::Money),
        arg: None,
    };
    static TOP_UP: Op = Op {
        code: "T",
        name: "Top-up",
        words: &["t", "topup"],
        help: "",
        value: Some(Figure::Money),
        arg: Some(Figure::Months),
    };
    static BUYOFF: Op =
        Op { code: "B", name: "Buy-off", words: &["b", "buyoff"], help: "", value: None, arg: None };

    fn savings() -> OpSet {
        OpSet::new(vec![&DEPOSIT, &WITHDRAW], &DEPOSIT, Some(&WITHDRAW))
    }

    /// `(code, amount, arg)` of each entry, and whether the cell appends.
    type Read = (Vec<(&'static str, f64, Option<i64>)>, bool);

    fn read(text: &str) -> Read {
        let (entries, append) = savings().parse(text).unwrap();
        (entries.iter().map(|e| (e.op.code, e.amount, e.arg)).collect(), append)
    }

    #[test]
    fn an_operation_is_any_unambiguous_start_of_its_name() {
        for text in ["@W=500", "@w=500", "@W 500", "@w500", "@withdraw=500", "@Withdrawal = 500", "@w: 500"] {
            assert_eq!(read(text), (vec![("W", 500.0, None)], false), "{text}");
        }
    }

    #[test]
    fn a_bare_figure_is_the_default_and_keeps_its_thousands() {
        assert_eq!(read("2,000").0, [("D", 2000.0, None)]);
        assert_eq!(read("@I=2,000").0, [("D", 2000.0, None)]);
        assert_eq!(read("-500").0, [("W", 500.0, None)]);
    }

    #[test]
    fn commas_split_only_before_a_space_or_an_operation() {
        assert_eq!(read("2,000, @W=500").0, [("D", 2000.0, None), ("W", 500.0, None)]);
        assert_eq!(read("2,000 @W=1,500").0, [("D", 2000.0, None), ("W", 1500.0, None)]);
        assert_eq!(read("@D=2,000; @W=500").0, [("D", 2000.0, None), ("W", 500.0, None)]);
        assert_eq!(read("@W=500, 2000").0, [("W", 500.0, None), ("D", 2000.0, None)]);
        assert_eq!(read("2000, 500").0, [("D", 2000.0, None), ("D", 500.0, None)]);
    }

    #[test]
    fn a_leading_plus_adds_rather_than_replaces() {
        assert_eq!(read("+@W=500"), (vec![("W", 500.0, None)], true));
        assert_eq!(read("+500"), (vec![("D", 500.0, None)], false));
    }

    #[test]
    fn nothing_is_no_entries_and_a_zero_is_kept() {
        for text in ["", "-", "—"] {
            assert_eq!(read(text), (vec![], false));
        }
        assert_eq!(read("@W=0").0, [("W", 0.0, None)]);
    }

    #[test]
    fn nonsense_and_misplaced_commas_are_refused_in_a_sentence() {
        let refusal = |t: &str| savings().parse(t).unwrap_err();
        assert!(refusal("@X=5").contains("not an operation"));
        assert!(refusal("abc").contains("not a number"));
        assert!(refusal("@W").contains("needs a figure"));
        assert!(refusal("@W=-5").contains("positive figure"));
        for text in ["1,00", "500,2000", "@W=5,00"] {
            assert!(refusal(text).contains("wrong places"), "{text}");
        }
    }

    #[test]
    fn loan_operations_carry_their_own_figures() {
        let loans = OpSet::new(vec![&DEPOSIT, &TOP_UP, &BUYOFF], &DEPOSIT, None);
        let (e, _) = loans.parse("@T=20,000/12").unwrap();
        assert_eq!((e[0].amount, e[0].arg), (20000.0, Some(12)));
        let (e, _) = loans.parse("@topup 20000 / 12m").unwrap();
        assert_eq!((e[0].amount, e[0].arg), (20000.0, Some(12)));
        assert!(loans.parse("@T=20000").unwrap_err().contains("months too"));
        assert!(loans.parse("@B=5000").unwrap_err().contains("takes no figure"));
        assert!(loans.parse("-500").unwrap_err().contains("more than zero"));
    }

    #[test]
    fn formatting_reads_back_as_the_same_entries() {
        for text in ["2,000", "@D=2,000, @W=500", "@W=500", "@D=1,234.50, @D=10"] {
            let (entries, _) = savings().parse(text).unwrap();
            assert_eq!(savings().format(&entries), text);
        }
        let (entries, _) = savings().parse("2000, @w=500").unwrap();
        assert_eq!(savings().format(&entries), "@D=2,000, @W=500");
    }
}
