//! Dates as the journal stores them: `YYYY-MM-DD` text.
//!
//! Month arithmetic follows `dateutil.relativedelta(months=n)`, which is what
//! wrote every due date in the journal: the day is clamped to the end of a
//! shorter month (31 Jan + 1 month = 28 Feb), and stepping again from a clamped
//! date stays on the clamped day (28 Feb + 1 month = 28 Mar). Callers that step
//! month by month therefore drift exactly as the history did.

use chrono::{Datelike, Local, Months, NaiveDate};

pub const FORMAT: &str = "%Y-%m-%d";

/// Parse a stored date, tolerating a time suffix (`2026-03-01 10:00:00`).
pub fn parse(text: &str) -> Option<NaiveDate> {
    let head = text.trim().get(..10)?;
    NaiveDate::parse_from_str(head, FORMAT).ok()
}

pub fn format(date: NaiveDate) -> String {
    date.format(FORMAT).to_string()
}

/// Today's date, local time — what `datetime.now()` gave the history.
pub fn today() -> String {
    format(Local::now().date_naive())
}

/// Now, local time, as the journal stamps `created_at`.
pub fn now_stamp() -> String {
    Local::now().format("%Y-%m-%d %H:%M:%S").to_string()
}

/// `relativedelta(months=n)`: clamps to the last day of a shorter month.
pub fn add_months(date: NaiveDate, months: i32) -> NaiveDate {
    let result = if months >= 0 {
        date.checked_add_months(Months::new(months as u32))
    } else {
        date.checked_sub_months(Months::new(months.unsigned_abs()))
    };
    result.unwrap_or(date)
}

/// [`add_months`] on stored text. `None` when the text is not a date.
pub fn add_months_str(text: &str, months: i32) -> Option<String> {
    parse(text).map(|d| format(add_months(d, months)))
}

pub fn add_days(date: NaiveDate, days: i64) -> NaiveDate {
    date + chrono::Duration::days(days)
}

/// The first ten characters — the date part of a stored value.
pub fn date_part(text: &str) -> &str {
    text.get(..10).unwrap_or(text)
}

/// `'2026-03-14'` → `'2026-03'`: the grid column a row belongs to.
pub fn month_key(text: &str) -> String {
    text.get(..7).unwrap_or(text).to_string()
}

fn first_of(key: &str) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(&format!("{}-01", key.get(..7)?), FORMAT).ok()
}

/// Every month key from `start` to `end` inclusive. An end before the start is
/// an empty range rather than an error — a picker produces one mid-choice.
pub fn month_range(start: &str, end: &str) -> Vec<String> {
    let (Some(mut cursor), Some(last)) = (first_of(start), first_of(end)) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    while cursor <= last {
        out.push(cursor.format("%Y-%m").to_string());
        cursor = add_months(cursor, 1);
    }
    out
}

/// The first and last day covering a list of month keys (sorted).
pub fn month_bounds(months: &[String]) -> Option<(String, String)> {
    let first = first_of(months.first()?)?;
    let last = add_days(add_months(first_of(months.last()?)?, 1), -1);
    Some((format(first), format(last)))
}

/// `'2026-03'` → `'Mar 2026'`.
pub fn month_label(key: &str) -> String {
    first_of(key).map(|d| d.format("%b %Y").to_string()).unwrap_or_else(|| key.to_string())
}

/// English month name, 1-based.
pub fn month_name(month: u32) -> &'static str {
    const NAMES: [&str; 12] = [
        "January", "February", "March", "April", "May", "June", "July", "August",
        "September", "October", "November", "December",
    ];
    NAMES.get(month.wrapping_sub(1) as usize).copied().unwrap_or("")
}

/// A date's day-of-month clamped into another month: the date a monthly
/// schedule falls on in `month`.
pub fn day_in_month(month: &str, day: u32) -> String {
    let Some(first) = first_of(month) else { return month.to_string() };
    let last_day = add_days(add_months(first, 1), -1).day();
    format(first.with_day(day.min(last_day).max(1)).unwrap_or(first))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn months_clamp_and_then_drift_like_relativedelta() {
        let jan31 = parse("2026-01-31").unwrap();
        let feb = add_months(jan31, 1);
        assert_eq!(format(feb), "2026-02-28");
        assert_eq!(format(add_months(feb, 1)), "2026-03-28");
        assert_eq!(format(add_months(parse("2024-01-31").unwrap(), 1)), "2024-02-29");
        assert_eq!(format(add_months(parse("2026-03-15").unwrap(), -2)), "2026-01-15");
    }

    #[test]
    fn a_time_suffix_is_tolerated() {
        assert_eq!(parse("2026-03-01 10:00:00").map(format).as_deref(), Some("2026-03-01"));
        assert!(parse("").is_none());
        assert!(parse("garbage").is_none());
    }

    #[test]
    fn month_ranges_are_inclusive_and_never_backwards() {
        assert_eq!(month_range("2025-11", "2026-02"),
                   ["2025-11", "2025-12", "2026-01", "2026-02"]);
        assert!(month_range("2026-03", "2026-01").is_empty());
        let months = month_range("2026-01", "2026-02");
        assert_eq!(month_bounds(&months),
                   Some(("2026-01-01".into(), "2026-02-28".into())));
        assert_eq!(month_label("2026-03"), "Mar 2026");
        assert_eq!(day_in_month("2026-02", 31), "2026-02-28");
    }
}
