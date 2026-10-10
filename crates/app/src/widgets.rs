//! Small pieces every screen uses.

use eframe::egui::{self, Color32, CornerRadius, RichText, Stroke, Ui};
use loanmaster_core::money::shillings;

use crate::theme::{self, Palette};

/// A solid button in one of the palette's colours.
pub fn solid(ui: &mut Ui, text: &str, fill: Color32) -> egui::Response {
    ui.add(
        egui::Button::new(RichText::new(text).color(theme::text_on(fill)).strong())
            .fill(fill)
            .stroke(Stroke::NONE)
            .corner_radius(CornerRadius::same(6))
            .min_size(egui::vec2(0.0, 28.0)),
    )
}

/// The same, greyed and refusing clicks when `enabled` is false — with the
/// reason on hover.
pub fn solid_enabled(ui: &mut Ui, enabled: bool, text: &str, fill: Color32, why_not: &str) -> egui::Response {
    let fill = if enabled { fill } else { fill.gamma_multiply(0.45) };
    let button = egui::Button::new(RichText::new(text).color(theme::text_on(fill)).strong())
        .fill(fill)
        .stroke(Stroke::NONE)
        .corner_radius(CornerRadius::same(6))
        .min_size(egui::vec2(0.0, 28.0));
    let response = ui.add_enabled(enabled, button);
    if enabled { response } else { response.on_disabled_hover_text(why_not) }
}

/// A KPI tile: a large figure over a small caption. A tile that does
/// something says so with an arrow and lights up under the pointer.
pub fn stat_tile(ui: &mut Ui, p: &Palette, value: &str, caption: &str, value_colour: Color32, clickable: bool) -> egui::Response {
    let frame = egui::Frame::new()
        .fill(p.card)
        .stroke(Stroke::new(1.0, p.border))
        .corner_radius(CornerRadius::same(10))
        .inner_margin(egui::Margin::symmetric(16, 12));
    let inner = frame.show(ui, |ui| {
        ui.vertical(|ui| {
            ui.set_min_width(150.0);
            ui.spacing_mut().item_spacing.y = 2.0;
            ui.label(RichText::new(value).size(22.0).strong().color(value_colour));
            let caption = if clickable { format!("{caption}  ›") } else { caption.to_string() };
            ui.label(RichText::new(caption).small().color(if clickable { p.accent } else { p.muted }));
        });
    });
    let response = ui.interact(inner.response.rect, ui.id().with(caption), if clickable { egui::Sense::click() } else { egui::Sense::hover() });
    if clickable && response.hovered() {
        ui.painter().rect_stroke(inner.response.rect, CornerRadius::same(10), Stroke::new(1.5, p.accent), egui::StrokeKind::Inside);
        ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
    }
    response
}

/// A small coloured tag: RETIRED, SUSPENDED, OVERDUE.
pub fn badge(ui: &mut Ui, text: &str, colour: Color32) {
    egui::Frame::new()
        .fill(colour.gamma_multiply(0.15))
        .stroke(Stroke::new(1.0, colour.gamma_multiply(0.6)))
        .corner_radius(CornerRadius::same(4))
        .inner_margin(egui::Margin::symmetric(5, 1))
        .show(ui, |ui| {
            ui.label(RichText::new(text).small().strong().color(colour));
        });
}

/// A figure typed by a person: thousands separators allowed, finite only.
pub fn number(text: &str) -> Option<f64> {
    text.replace(',', "").trim().parse::<f64>().ok().filter(|v| v.is_finite())
}

/// An amount of money typed by a person: a positive [`number`].
pub fn amount(text: &str) -> Option<f64> {
    number(text).filter(|v| *v > 0.0)
}

/// Money for a table cell: whole shillings, a dash for nothing.
pub fn money_or_dash(value: f64) -> String {
    if value.abs() < 0.005 { "—".into() } else { shillings(value) }
}

/// A right-aligned label in a table cell.
pub fn right(ui: &mut Ui, text: impl Into<egui::WidgetText>) {
    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
        ui.label(text);
    });
}

/// A labelled text field with its error, if any, underneath.
pub fn field(ui: &mut Ui, label: &str, value: &mut String, hint: &str, error: Option<&str>) -> egui::Response {
    ui.label(label);
    let response = ui
        .vertical(|ui| {
            let response = ui.add(egui::TextEdit::singleline(value).hint_text(hint).desired_width(280.0));
            if let Some(error) = error {
                ui.label(RichText::new(error).small().color(theme::palette(ui.ctx()).danger));
            }
            response
        })
        .inner;
    ui.end_row();
    response
}

/// A date chooser. The picker works in `jiff` dates; the rest of the
/// application in `chrono` ones.
pub fn date(ui: &mut Ui, id: &str, value: &mut chrono::NaiveDate) -> egui::Response {
    use chrono::Datelike;
    let mut picked = jiff::civil::Date::new(value.year() as i16, value.month() as i8, value.day() as i8).unwrap_or_default();
    let response = ui.add(egui_extras::DatePickerButton::new(&mut picked).id_salt(id).format("%Y-%m-%d").calendar_week(false));
    if let Some(date) = chrono::NaiveDate::from_ymd_opt(picked.year() as i32, picked.month() as u32, picked.day() as u32) {
        *value = date;
    }
    response
}

/// How tall a dialog's body may grow before it scrolls, leaving its title and
/// its row of buttons on a small screen (1366 × 768 is common).
pub fn dialog_room(ctx: &egui::Context) -> f32 {
    (ctx.content_rect().height() - 190.0).max(220.0)
}

/// Make a table's rows take clicks anywhere on them: text that can be
/// selected would otherwise take a click on a name for itself.
pub fn clickable_rows(ui: &mut Ui) {
    ui.style_mut().interaction.selectable_labels = false;
}

/// Fill a table cell edge to edge, across the gap to its neighbours, so a
/// coloured row reads as one band.
pub fn fill_cell(ui: &Ui, fill: Color32) {
    let rect = ui.max_rect().expand2(egui::vec2(ui.spacing().item_spacing.x / 2.0 + 1.0, 2.0));
    let mut painter = ui.painter().clone();
    painter.set_clip_rect(rect);
    painter.rect_filled(rect, 0.0, fill);
}
