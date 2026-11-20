use super::files;
use crate::app::{Action, Notice};
use eframe::egui;
use loanmaster_core::{Error, Journal, Result, office};

pub struct Payroll {
    user: String,
    month: String,
    input: String,
    bank: String,
    reason: String,
    target: String,
    role: String,
    preview: Vec<office::Line>,
    message: String,
}
impl Payroll {
    pub fn new(j: &Journal) -> Result<Self> {
        Ok(Self {
            user: office::principal()?,
            month: j.today().format("%Y-%m").to_string(),
            input: String::new(),
            bank: String::new(),
            reason: String::new(),
            target: String::new(),
            role: "officer".into(),
            preview: Vec::new(),
            message: String::new(),
        })
    }
    fn result(&mut self, result: Result<()>, success: &str) {
        self.message = match result {
            Ok(()) => success.into(),
            Err(e) => e.to_string(),
        };
    }
    fn load(&mut self, path: &std::path::Path) -> Result<()> {
        let sheet = loanmaster_core::imports::excel::read_sheet(path)?;
        if !matches!(sheet.headers.len(),3|7) {
            return Err(Error::invalid(
                "Use PF, expected and deducted. Optionally append loan, savings, Christmas and benevolent amounts.",
            ));
        }
        let mut lines = Vec::new();
        for row in sheet.rows {
            let values = sheet
                .headers
                .iter()
                .map(|h| {
                    row.get(h)
                        .map(loanmaster_core::imports::excel::cell_text)
                        .unwrap_or_default()
                })
                .collect::<Vec<_>>();
            if values.iter().any(|v| v.contains(['\n', '\t', '\r'])) {
                return Err(Error::invalid(
                    "A payroll cell contains a line break or tab.",
                ));
            }
            lines.push(values.join("\t"));
        }
        self.input = lines.join("\n");
        self.preview.clear();
        Ok(())
    }
    pub fn show(&mut self, ctx: &egui::Context, j: &Journal, actions: &mut Vec<Action>) -> bool {
        let mut open = true;
        egui::Window::new("Payroll and month-end close").open(&mut open).vscroll(true).show(ctx,|ui|{
            let role=office::role(j,&self.user).unwrap_or_else(|e|e.to_string());
            ui.label(format!("Signed in as {} ({role})",self.user));
            ui.collapsing("Operator access",|ui|{
                if role=="unconfigured" {
                    if ui.button("Register this account as administrator").clicked(){self.result(office::set_role(j,&self.user,&self.user,"administrator"),"Administrator registered. Unlisted accounts have reviewer access.");}
                }else if role=="administrator"{
                    ui.label("Operating system account (as displayed on sign-in)");
                    ui.text_edit_singleline(&mut self.target);
                    ui.horizontal(|ui|{for r in ["administrator","officer","reviewer"] {ui.selectable_value(&mut self.role,r.to_string(),r);}});
                    if ui.button("Save account role").clicked(){self.result(office::set_role(j,&self.user,&self.target,&self.role),"Role saved.");}
                }
            });
            ui.horizontal(|ui|{ui.label("Month (YYYY-MM)");if ui.text_edit_singleline(&mut self.month).changed(){self.preview.clear();}});
            ui.label("Paste payroll columns in order: PF number, expected amount, actual deduction. Optionally append loan, savings, Christmas and benevolent amounts to check allocation.");
            if ui.text_edit_multiline(&mut self.input).changed(){self.preview.clear();}
            if ui.button("Import payroll workbook…").clicked() && let Some(path)=files::pick("Payroll workbook",&[("Excel",&["xlsx","xls"])]) {
                let result=self.load(&path);self.result(result,"Payroll loaded. Compare against the journal.");
            }
            if ui.button("Compare with postings").clicked(){
                self.preview.clear();
                match office::compare(j,&self.month,&self.input){Ok(rows)=>{self.preview=rows;self.message.clear();},Err(e)=>self.message=e.to_string()}
            }
            if !self.preview.is_empty(){
                egui::ScrollArea::vertical().max_height(175.0).show(ui,|ui|{
                    egui::Grid::new("payroll_comparison").striped(true).show(ui,|ui|{
                        for h in ["PF","Expected","Deducted","Posted"]{ui.strong(h);} ui.end_row();
                        for l in &self.preview{ui.label(&l.pf);for value in [l.expected,l.payroll,l.posted]{ui.label(format!("{}.{:02}",value/100,value%100));}
                            if let Some(payroll)=l.payroll_by_fund && payroll!=l.posted_by_fund{
                                ui.label(format!("Allocation differs (loan, savings, Christmas, benevolent), in cents: payroll {payroll:?}, posted {:?}",l.posted_by_fund));
                            }ui.end_row();}
                    });
                });
            }
            ui.horizontal(|ui|{ui.label("Bank remittance (KSh)");ui.text_edit_singleline(&mut self.bank);});
            ui.label("Explanation or reopening reason");ui.text_edit_singleline(&mut self.reason);
            if ui.add_enabled(!self.preview.is_empty(),egui::Button::new("Sign off and close month")).clicked(){
                self.result(office::close(j,&self.user,&self.month,&self.input,&self.bank,&self.reason),"Month closed. The signed-off figures are in the close history.");
            }
            if ui.button("Request or approve reopening").clicked(){self.result(office::reopen(j,&self.user,&self.month,&self.reason),"Decision recorded in close history.");}
            if ui.button("Export signed close…").clicked() {
                let result=office::close_report(j,&self.month).and_then(|table| {
                    if let Some(path)=files::save_as("Save signed close","Payroll close.pdf",&[("PDF","pdf"),("Excel","xlsx")]) {
                        loanmaster_reports::write_custom_table(&table,&loanmaster_reports::Branding::of(j),&path).map_err(|e| Error::invalid(e.to_string()))?;
                        files::saved(actions,"Payroll close",&path);
                    }
                    Ok(())
                });
                if let Err(e)=result{self.message=e.to_string();}
            }
            if ui.button("View close history").clicked(){
                let history=(||->Result<String>{
                    let mut q=j.conn().prepare("SELECT recorded_at,event,operator,reason FROM period_events WHERE month=? ORDER BY id")?;
                    let rows=q.query_map([&self.month],|r|Ok(format!("{}  {}  {}  {}",r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,String>(3)?)))?.collect::<std::result::Result<Vec<_>,_>>()?;
                    Ok(if rows.is_empty(){"No decisions recorded.".into()}else{rows.join("\n")})
                })();
                actions.push(Action::Notice(Notice::info("Month-end history",history.unwrap_or_else(|e|e.to_string()))));
            }
            ui.label(&self.message);
        });
        open
    }
}
