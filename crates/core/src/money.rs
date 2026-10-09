//! Amounts: rounding exactly as the journal was written, and display.
//!
//! Money is `f64`, as it is in every journal written so far. What matters for
//! continuity is that rounding agrees to the cent with the figures already
//! stored, and those were rounded by Python's `round(x, 2)`: round-half-even on
//! the exact binary value, so `0.125` becomes `0.12` and `2.675` becomes `2.67`.
//! `f64::round` would give `0.13` and `2.68`. Rust's fixed-precision formatting
//! rounds the exact value the same way Python does, so it is the definition.

/// Round to two places, as `round(x, 2)` does in the journal's history.
pub fn round2(value: f64) -> f64 {
    round_places(value, 2)
}

/// Round to `places` decimal places, half-to-even on the exact binary value.
pub fn round_places(value: f64, places: usize) -> f64 {
    if !value.is_finite() {
        return value;
    }
    format!("{value:.places$}").parse().unwrap_or(value)
}

/// Equal to the cent.
pub fn cents_eq(a: f64, b: f64) -> bool {
    round2(a) == round2(b)
}

/// An amount for display: thousands-separated, `decimals` places.
///
/// Display only — the stored value is untouched. Zero formats as `0`, never
/// as an empty string; screens that want a dash for zero say so themselves.
pub fn format_money(value: f64, decimals: usize) -> String {
    let value = if value.is_finite() { value } else { 0.0 };
    let text = format!("{value:.decimals$}");
    let (sign, digits) = match text.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", text.as_str()),
    };
    let (whole, fraction) = match digits.split_once('.') {
        Some((w, f)) => (w, Some(f)),
        None => (digits, None),
    };
    let mut grouped = String::with_capacity(whole.len() + whole.len() / 3);
    for (i, ch) in whole.chars().enumerate() {
        if i > 0 && (whole.len() - i) % 3 == 0 {
            grouped.push(',');
        }
        grouped.push(ch);
    }
    match fraction {
        Some(f) => format!("{sign}{grouped}.{f}"),
        None => format!("{sign}{grouped}"),
    }
}

/// Whole shillings, the way every grid and statement shows them.
pub fn shillings(value: f64) -> String {
    format_money(value, 0)
}

/// Whole shillings when whole, two places otherwise — how a cell writes a
/// figure back so it parses to the same value.
pub fn figure(value: f64) -> String {
    let value = round2(value);
    if value == value.trunc() { format_money(value, 0) } else { format_money(value, 2) }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rounding_matches_the_journals_history() {
        // Each pair was produced by Python's round(x, 2).
        for (x, want) in [
            (0.125, 0.12), (0.375, 0.38), (2.675, 2.67), (1.005, 1.0), (0.25, 0.25),
            (76411.70999999999, 76411.71), (1234.565, 1234.57), (0.115, 0.12),
            (-0.125, -0.12), (5.0 / 3.0, 1.67),
        ] {
            assert_eq!(round2(x), want, "round2({x})");
        }
    }

    #[test]
    fn money_is_grouped_by_thousands() {
        assert_eq!(format_money(1234567.0, 0), "1,234,567");
        assert_eq!(format_money(1234567.891, 2), "1,234,567.89");
        assert_eq!(format_money(999.0, 0), "999");
        assert_eq!(format_money(-4000.0, 0), "-4,000");
        assert_eq!(format_money(0.0, 0), "0");
        assert_eq!(format_money(f64::NAN, 0), "0");
    }

    #[test]
    fn a_figure_reads_back_as_itself() {
        assert_eq!(figure(2000.0), "2,000");
        assert_eq!(figure(1234.5), "1,234.50");
    }
}
