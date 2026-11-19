//! The help panel: what the grid does, and what the current tab's cells
//! accept. The operations are read from the tab's own table, so the help
//! cannot list one the tab does not understand.

use eframe::egui::{self, RichText};
use loanmaster_core::grid::ops::OpSet;

use crate::theme;

/// Which tab the help is for: the examples and rules differ.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Savings,
    Christmas,
    Benevolent,
    Loans,
}

const FUND_EXAMPLES: &[(&str, &str)] = &[
    ("2,000", "Sets the month's deposit to 2,000. A withdrawal already in the month is kept."),
    ("-500  or  @W=500", "Sets the month's withdrawal to 500. The deposit is kept."),
    ("2,000, @W=500", "Sets both."),
    ("@W=0", "Removes the month's withdrawal (@D=0 removes the deposit)."),
    ("+@W=500", "Adds a second withdrawal beside the one already there."),
    ("(empty)  or  -", "Clears the whole month."),
];

const SAVINGS_SUSPEND: (&str, &str) = (
    "@S=3",
    "Suspends the member's contributions for 3 months from this one. Those months show ⏸, take no deposit, and \
     Fill Forward passes over them; resume early or remove it in the member's Savings tab.",
);

const CHRISTMAS_NOTE: &str = "Withdrawals are refused outside the unlock month set in Settings; use the member's Christmas tab to override.";

const BENEVOLENT_EXAMPLES: &[(&str, &str)] = &[
    ("300", "Sets the month's contribution to 300. A payout is kept."),
    ("@P=10,000", "Sets the month's payout to 10,000 (recorded as a withdrawal). The contribution is kept."),
    ("300, @P=10,000", "Sets both."),
    ("@P=0", "Removes the month's payout."),
    ("(empty)  or  -", "Clears the whole month."),
];

const LOAN_EXAMPLES: &[(&str, &str)] = &[
    ("5,750", "Takes the month's deduction at 5,750 — or corrects one already taken."),
    ("@T=20,000/12", "Tops the loan up by 20,000, re-termed over 12 months."),
    ("@B", "Buys the loan off: settles principal and all interest."),
    ("@S=3", "Suspends deductions for 3 months from this one."),
    ("(empty)", "Removes the loan's most recent deduction."),
];

const FUND_MULTI: &str = "Every month is assumed to have its deposit; a withdrawal happens as well. So what you type \
    replaces only the kinds it names — a bare figure is the deposit, @W the withdrawal — and leaves the rest of the \
    month alone. Fill Forward treats a month with a withdrawal but no deposit as missed. Separate entries with commas: \
    2,000, @W=500. A comma splits entries when a space or an @ follows it, so 2,000 stays two thousand. A month \
    holding more than one entry is shown with every entry prefixed (@D=2,000, @W=500). Start with + to add another \
    entry of the same kind.";

const LOAN_MULTI: &str = "A loan month takes a deduction or one operation, and operations go only in the month the \
    loan is due next (its Due column). Each moves the loan's schedule, so save one before typing the next. Months \
    before the due date, and months after it while it is unpaid, are locked — hover for why.";

const DELETE_HOW: &str = "Right-click a month's heading — or select whole columns and press Delete — to delete that \
    month for the rows shown. A preview says what will happen before anything is staged. ";

const FUND_DELETING: &str = "Every deposit and withdrawal in the month goes, balances after it are recalculated, and \
    the month becomes a missed month that Fill Forward will offer again. On Savings and Christmas, a deletion that \
    would leave a later withdrawal bigger than the savings it came from is refused for that member.";

const LOAN_DELETING: &str = "A loan's last deduction is un-run: its interest returns to unearned and the month falls \
    due again. An earlier one becomes missed: its interest stays charged, later deductions are re-split interest \
    first, and the loan runs one instalment longer. A missed month shows as a red dash — type a figure there to \
    record it. Buy-offs, and a loan's last deduction before a top-up, are refused: use the member's ledger. Tick \
    Include paid loans to work on loans already repaid.";

const EDITING: &[(&str, &str)] = &[
    ("Type", "Replace a cell. A bare figure is the tab's usual entry."),
    ("F2 / Enter / double-click", "Edit a cell starting from what it holds — every entry in the month, written out."),
    ("Enter / Tab", "Stage what was typed and move down / right. Esc abandons it."),
    ("Arrow keys", "Move the selection. Shift-click selects a block, Ctrl-click adds cells."),
    ("Delete", "Clear the selected cells."),
    ("Ctrl+C / Ctrl+V", "Copy to or paste from Excel; pastes run across and down from the selected cell."),
    ("Ctrl+Z", "Take back the last unsaved edit."),
    ("Save Changes", "Write everything staged, as one batch. Undo Last Save reverses it exactly."),
];

/// Three ways to write the tab's first operation that is not the default.
fn spellings(ops: &OpSet) -> String {
    let Some(op) = ops.ops.iter().find(|o| !ops.is_default(o)) else { return String::new() };
    let longest = op.words.iter().max_by_key(|w| w.len()).copied().unwrap_or(op.code);
    format!("@{}, @{} and @{longest}", op.code.to_lowercase(), op.code)
}

fn heading(ui: &mut egui::Ui, text: &str) {
    ui.add_space(8.0);
    ui.label(RichText::new(text).strong().size(15.0));
    ui.separator();
}

fn pairs(ui: &mut egui::Ui, id: &str, rows: &[(String, String)], code: bool) {
    let p = theme::palette(ui.ctx());
    egui::Grid::new(id).num_columns(2).spacing([14.0, 6.0]).show(ui, |ui| {
        for (left, right) in rows {
            let left = if code { RichText::new(left).monospace().color(p.accent) } else { RichText::new(left).strong() };
            ui.label(left);
            ui.add(egui::Label::new(right.as_str()).wrap());
            ui.end_row();
        }
    });
}

fn owned(rows: &[(&str, &str)]) -> Vec<(String, String)> {
    rows.iter().map(|(a, b)| (a.to_string(), b.to_string())).collect()
}

/// The help window. `open` turns false when it is closed.
pub fn show(ctx: &egui::Context, open: &mut bool, kind: Kind, label: &str, ops: &OpSet) {
    let p = theme::palette(ctx);
    egui::Window::new(format!("Ledger Grid Help — {label}"))
        .id(egui::Id::new("grid_help"))
        .open(open)
        .default_size([620.0, 560.0])
        .collapsible(false)
        .show(ctx, |ui| {
            egui::ScrollArea::vertical().show(ui, |ui| {
                ui.set_max_width(600.0);
                heading(ui, &format!("{label}: what a cell accepts"));
                let rows: Vec<(String, String)> = ops.help_rows().into_iter().map(|(a, b)| (a, b.to_string())).collect();
                pairs(ui, "grid_help_ops", &rows, true);
                ui.add_space(4.0);
                ui.label(RichText::new(format!("Write an operation as @ and the start of its name — {} are the same. The = is optional.", spellings(ops))).color(p.muted));

                heading(ui, "Examples");
                let mut examples = owned(match kind {
                    Kind::Savings | Kind::Christmas => FUND_EXAMPLES,
                    Kind::Benevolent => BENEVOLENT_EXAMPLES,
                    Kind::Loans => LOAN_EXAMPLES,
                });
                if kind == Kind::Christmas {
                    examples.push((String::new(), CHRISTMAS_NOTE.to_string()));
                }
                if kind == Kind::Savings {
                    examples.push((SAVINGS_SUSPEND.0.to_string(), SAVINGS_SUSPEND.1.to_string()));
                }
                pairs(ui, "grid_help_examples", &examples, true);

                heading(ui, if kind == Kind::Loans { "One thing per month" } else { "Several entries in one month" });
                ui.add(egui::Label::new(if kind == Kind::Loans { LOAN_MULTI } else { FUND_MULTI }).wrap());

                heading(ui, "Deleting months");
                ui.add(egui::Label::new(format!("{DELETE_HOW}{}", if kind == Kind::Loans { LOAN_DELETING } else { FUND_DELETING })).wrap());

                heading(ui, "Editing");
                pairs(ui, "grid_help_editing", &owned(EDITING), false);

                heading(ui, "Colours");
                egui::Grid::new("grid_help_colours").num_columns(2).spacing([14.0, 6.0]).show(ui, |ui| {
                    for (colour, name, meaning) in [
                        (p.warning, "Orange", "Staged — typed but not saved yet."),
                        (p.danger, "Red", "Refused when saving; hover it for the reason. On the Loans tab, a red dash is a missed month."),
                        (p.muted, "Grey", "Locked; hover it for why, or double-click to open the member's ledger."),
                    ] {
                        ui.label(RichText::new(name).strong().color(colour));
                        ui.add(egui::Label::new(meaning).wrap());
                        ui.end_row();
                    }
                });
            });
        });
}
