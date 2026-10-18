//! One member's ledger: their loans, savings, Christmas and benevolent funds.

use std::collections::HashMap;

use chrono::NaiveDate;
use eframe::egui::{self, RichText};
use loanmaster_core::db::model::{Enrolment, Fund, FundRow, LOAN_ACTIVE, LedgerRow, Loan, Member, SavingsSuspension, Suspension, event};
use loanmaster_core::loans::service::loan_rate;
use loanmaster_core::money::{round2, shillings, total};
use loanmaster_core::{Journal, Result, dates, funds};

use crate::app::Action;
use crate::theme;
use crate::widgets;

mod funds_tab;
mod loans;

pub use loans::NewLoan;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Tab {
    #[default]
    Loans,
    Fund(Fund),
}

/// One loan and its history, as the screen shows it.
#[derive(Debug, Clone)]
pub struct LoanView {
    pub loan: Loan,
    pub rows: Vec<LedgerRow>,
    /// Principal plus all the interest the loan was written for, less what
    /// was paid — after each row. At the loan's own rate, to the cent.
    pub gross: Vec<f64>,
    pub spans: Vec<Suspension>,
    pub overdue: bool,
    pub rate: f64,
}

#[derive(Debug, Clone)]
pub struct MemberData {
    pub member: Member,
    pub loans: Vec<LoanView>,
    pub funds: [Vec<FundRow>; 3],
    pub balances: [f64; 3],
    pub enrolment: Option<Enrolment>,
    /// When the member's savings contributions are, or were, suspended.
    pub savings_suspensions: Vec<SavingsSuspension>,
    pub suggested: [f64; 3],
    pub unlock_month: u32,
    pub default_rate: f64,
}

pub fn slot(fund: Fund) -> usize {
    match fund {
        Fund::Savings => 0,
        Fund::Christmas => 1,
        Fund::Benevolent => 2,
    }
}

fn gross_after(rows: &[LedgerRow], rate: f64) -> Vec<f64> {
    let mut gross = 0.0;
    rows.iter()
        .map(|row| {
            match row.event_type.as_str() {
                event::LOAN_ISSUED | event::TOP_UP => gross = round2(gross + row.added + round2(row.added * rate)),
                event::REPAYMENT | event::BUYOFF => gross = round2(gross - row.deducted),
                _ => {}
            }
            gross
        })
        .collect()
}

pub fn load(journal: &Journal, id: i64) -> Result<MemberData> {
    let member = journal.members().get(id)?.ok_or(loanmaster_core::Error::MemberNotFound(id))?;
    let today = dates::today();
    let mut loans = Vec::new();
    for loan in journal.loans().for_member(id)? {
        let rows = journal.ledger().for_loan(id, &loan.reference)?;
        let rate = loan_rate(journal, &loan);
        loans.push(LoanView {
            gross: gross_after(&rows, rate),
            spans: journal.loans().suspensions_for_loan(loan.id)?,
            overdue: loan.status == LOAN_ACTIVE && !loan.is_suspended && loan.next_due_date.as_deref().is_some_and(|d| !d.is_empty() && d < today.as_str()),
            rate,
            rows,
            loan,
        });
    }
    let fund_rows = [funds::rows(journal, Fund::Savings, id)?, funds::rows(journal, Fund::Christmas, id)?, funds::rows(journal, Fund::Benevolent, id)?];
    let balances = Fund::ALL.map(|f| journal.funds().balance(f, id).unwrap_or(0.0));
    Ok(MemberData {
        suggested: Fund::ALL.map(|f| funds::suggested_increment(journal, f, id).unwrap_or(0.0)),
        enrolment: journal.funds().enrolment(id)?,
        savings_suspensions: journal.funds().suspensions(id)?,
        unlock_month: journal.settings().christmas_unlock_month(),
        default_rate: journal.settings().default_interest_rate(),
        funds: fund_rows,
        balances,
        member,
        loans,
    })
}

pub struct MemberScreen {
    pub id: i64,
    pub tab: Tab,
    data: Option<(u64, MemberData)>,
    problem: Option<String>,
    pub new_loan: NewLoan,
    /// How many months the Deduct button takes, per loan.
    pub deduct_times: HashMap<String, u32>,
    /// The monthly figure typed for Savings, Christmas and benevolent.
    pub monthly: [String; 3],
    /// A loan to bring into view on the next frame.
    pub scroll_to: Option<String>,
}

impl MemberScreen {
    pub fn new(id: i64) -> MemberScreen {
        MemberScreen {
            id,
            tab: Tab::Loans,
            data: None,
            problem: None,
            new_loan: NewLoan::default(),
            deduct_times: HashMap::new(),
            monthly: Default::default(),
            scroll_to: None,
        }
    }

    pub fn show(&mut self, ui: &mut egui::Ui, journal: &Journal, revision: u64, last_date: NaiveDate, actions: &mut Vec<Action>) {
        if self.data.as_ref().is_none_or(|(r, _)| *r != revision) {
            match load(journal, self.id) {
                Ok(data) => {
                    for fund in Fund::ALL {
                        let i = slot(fund);
                        if self.monthly[i].is_empty() && data.suggested[i] > 0.0 {
                            self.monthly[i] = loanmaster_core::money::figure(data.suggested[i]);
                        }
                    }
                    self.data = Some((revision, data));
                    self.problem = None;
                }
                Err(e) => {
                    self.problem = Some(e.to_string());
                    self.data = None;
                }
            }
        }
        let p = theme::palette(ui.ctx());
        let Some((loaded, data)) = self.data.take() else {
            egui::CentralPanel::default().show(ui, |ui| {
                if ui.button("⬅ Dashboard").clicked() {
                    actions.push(Action::ShowDashboard);
                }
                ui.colored_label(p.danger, format!("This member could not be read: {}", self.problem.clone().unwrap_or_default()));
            });
            return;
        };

        egui::Panel::top("member_header").frame(egui::Frame::new().fill(p.card).inner_margin(egui::Margin::symmetric(18, 10))).show(ui, |ui| {
            self.header(ui, &data, actions);
        });
        egui::CentralPanel::default().frame(egui::Frame::new().fill(p.background).inner_margin(egui::Margin::symmetric(14, 10))).show(ui, |ui| {
            match self.tab {
                Tab::Loans => loans::show(self, ui, journal, &data, last_date, actions),
                Tab::Fund(fund) => funds_tab::show(self, ui, fund, &data, last_date, actions),
            }
        });
        self.data = Some((loaded, data));
    }

    fn header(&mut self, ui: &mut egui::Ui, data: &MemberData, actions: &mut Vec<Action>) {
        let p = theme::palette(ui.ctx());
        let member = &data.member;
        ui.horizontal(|ui| {
            if ui.button("⬅ Dashboard").clicked() {
                actions.push(Action::ShowDashboard);
            }
            ui.add_space(8.0);
            ui.label(RichText::new(&member.name).size(20.0).strong());
            if !member.is_active() {
                let status = if member.employment_status.is_empty() { "Retired".to_string() } else { member.employment_status.clone() };
                let since = member.retired_date.as_deref().map(|d| format!(" since {d}")).unwrap_or_default();
                widgets::badge(ui, &format!("{}{since}", status.to_uppercase()), p.warning);
            }
            let details: Vec<&str> = [member.pf_no.as_str(), member.phone.as_str(), member.email.as_str()].into_iter().filter(|s| !s.is_empty()).collect();
            ui.label(RichText::new(details.join("  ·  ")).color(p.muted));
        });
        ui.add_space(6.0);
        ui.horizontal(|ui| {
            ui.style_mut().wrap_mode = Some(egui::TextWrapMode::Extend);
            let owed = total(data.loans.iter().filter(|l| l.loan.status == LOAN_ACTIVE).map(|l| l.rows.last().map(|r| r.balance).unwrap_or(l.loan.balance)));
            for (caption, value) in [("Owed on loans", owed), ("Savings", data.balances[0]), ("Christmas", data.balances[1]), ("Benevolent", data.balances[2])] {
                egui::Frame::new().fill(p.background).corner_radius(8).inner_margin(egui::Margin::symmetric(12, 6)).show(ui, |ui| {
                    ui.vertical(|ui| {
                        ui.spacing_mut().item_spacing.y = 0.0;
                        ui.label(RichText::new(shillings(value)).strong().size(16.0));
                        ui.label(RichText::new(caption).small().color(p.muted));
                    });
                });
            }
        });
        ui.add_space(6.0);
        ui.horizontal(|ui| {
            let loans = data.loans.iter().filter(|l| l.loan.status == LOAN_ACTIVE).count();
            for (tab, title) in [
                (Tab::Loans, format!("Loans ({loans} active)")),
                (Tab::Fund(Fund::Savings), "Savings / Shares".to_string()),
                (Tab::Fund(Fund::Christmas), "Christmas".to_string()),
                (Tab::Fund(Fund::Benevolent), "Benevolent".to_string()),
            ] {
                let selected = self.tab == tab;
                let text = RichText::new(title).strong().color(if selected { p.accent } else { p.muted });
                let response = ui.add(egui::Button::new(text).fill(if selected { p.selected } else { egui::Color32::TRANSPARENT }).stroke(egui::Stroke::NONE));
                if selected {
                    let rect = response.rect;
                    ui.painter().hline(rect.x_range(), rect.bottom(), egui::Stroke::new(2.0, p.accent));
                }
                if response.clicked() {
                    self.tab = tab;
                }
            }
        });
    }
}
