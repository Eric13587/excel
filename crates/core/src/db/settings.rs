//! The `settings` table: per-journal configuration, typed.
//!
//! Every value is stored as TEXT. This is the one place that knows the key
//! names, their types and their defaults; callers ask for a typed value.

use rusqlite::{Connection, OptionalExtension, params};

use crate::error::Result;

pub const ORG_NAME: &str = "org_name";
pub const ORG_LOGO_PATH: &str = "org_logo_path";
pub const APP_THEME: &str = "app_theme";
pub const FY_START_MONTH: &str = "fy_start_month";
pub const DEDUCT_SAME_MONTH: &str = "deduct_same_month";
pub const CHRISTMAS_UNLOCK_MONTH: &str = "christmas_unlock_month";
pub const DEFAULT_SAVINGS_INCREMENT: &str = "default_savings_increment";
pub const DEFAULT_CHRISTMAS_INCREMENT: &str = "default_christmas_increment";
pub const DEFAULT_INTEREST_RATE: &str = "default_interest_rate";
pub const EXCEL_HEADER_BG: &str = "excel_header_bg";
pub const EXCEL_TOTAL_BG: &str = "excel_total_bg";

/// What applies when a journal has never had a key written. A journal created
/// by either version has every key, so these matter only for odd ones.
pub fn default_for(key: &str) -> &'static str {
    match key {
        APP_THEME => "Light",
        FY_START_MONTH => "November",
        DEDUCT_SAME_MONTH => "false",
        CHRISTMAS_UNLOCK_MONTH => "12",
        DEFAULT_SAVINGS_INCREMENT => "2500",
        DEFAULT_CHRISTMAS_INCREMENT => "0",
        DEFAULT_INTEREST_RATE => "0.15",
        EXCEL_HEADER_BG => "#E2E8F0",
        EXCEL_TOTAL_BG => "#F1F5F9",
        _ => "",
    }
}

pub struct Settings<'a>(pub(crate) &'a Connection);

impl Settings<'_> {
    /// The raw value, or the key's default.
    pub fn get(&self, key: &str) -> Result<String> {
        let value: Option<String> = self
            .0
            .query_row("SELECT value FROM settings WHERE key=?", [key], |r| r.get(0))
            .optional()?
            .flatten();
        Ok(value.unwrap_or_else(|| default_for(key).to_string()))
    }

    /// Write a setting. Does not commit — the caller owns the transaction.
    pub fn set(&self, key: &str, value: &str) -> Result<()> {
        self.0.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            params![key, value],
        )?;
        Ok(())
    }

    fn text(&self, key: &str) -> String {
        self.get(key).unwrap_or_else(|_| default_for(key).to_string())
    }

    fn boolean(&self, key: &str) -> bool {
        matches!(self.text(key).trim().to_lowercase().as_str(), "true" | "1" | "yes" | "on")
    }

    fn number(&self, key: &str) -> f64 {
        let raw = self.text(key);
        raw.trim().parse::<f64>().unwrap_or_else(|_| {
            if !raw.trim().is_empty() {
                log::warn!("Setting {key} is not a number ({raw:?}); using the default");
            }
            default_for(key).parse().unwrap_or(0.0)
        })
    }

    pub fn org_name(&self) -> String {
        self.text(ORG_NAME).trim().to_string()
    }

    pub fn org_logo_path(&self) -> String {
        self.text(ORG_LOGO_PATH)
    }

    /// `Light` or `Dark`.
    pub fn app_theme(&self) -> String {
        let theme = self.text(APP_THEME);
        if theme.is_empty() { default_for(APP_THEME).into() } else { theme }
    }

    /// Financial-year start month, 1–12.
    pub fn fy_start_month(&self) -> u32 {
        let name = self.text(FY_START_MONTH);
        (1..=12)
            .find(|m| crate::dates::month_name(*m).eq_ignore_ascii_case(name.trim()))
            .unwrap_or(11)
    }

    /// True when a loan's first deduction falls in its month of issue.
    pub fn deduct_same_month(&self) -> bool {
        self.boolean(DEDUCT_SAME_MONTH)
    }

    /// The month (1–12) in which Christmas withdrawals are permitted.
    pub fn christmas_unlock_month(&self) -> u32 {
        let month = self.number(CHRISTMAS_UNLOCK_MONTH) as u32;
        if (1..=12).contains(&month) { month } else { 12 }
    }

    pub fn default_savings_increment(&self) -> f64 {
        self.number(DEFAULT_SAVINGS_INCREMENT)
    }

    pub fn default_christmas_increment(&self) -> f64 {
        self.number(DEFAULT_CHRISTMAS_INCREMENT)
    }

    /// The rate a new loan is written at unless another is given.
    pub fn default_interest_rate(&self) -> f64 {
        self.number(DEFAULT_INTEREST_RATE)
    }

    pub fn excel_header_bg(&self) -> String {
        let value = self.text(EXCEL_HEADER_BG);
        if value.is_empty() { default_for(EXCEL_HEADER_BG).into() } else { value }
    }

    pub fn excel_total_bg(&self) -> String {
        let value = self.text(EXCEL_TOTAL_BG);
        if value.is_empty() { default_for(EXCEL_TOTAL_BG).into() } else { value }
    }
}
