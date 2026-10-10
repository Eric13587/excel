//! A list of columns to tick and put in order, as every report dialog offers.

use std::collections::HashMap;

use eframe::egui;

use crate::theme;

#[derive(Debug, Clone, Default)]
pub struct Columns {
    /// `(key, heading, shown)`, in the order chosen.
    items: Vec<(String, String, bool)>,
    selected: Option<usize>,
    /// Ticks by key, so a column keeps its tick when the list is offered
    /// again — choosing another quarter renames only the month columns.
    remembered: HashMap<String, bool>,
}

impl Columns {
    /// Columns under their own names, every one ticked.
    pub fn named(names: &[String]) -> Columns {
        let mut columns = Columns::default();
        columns.offer(names);
        columns
    }

    /// Columns by key and heading, ticked as given.
    pub fn keyed<'a>(items: impl IntoIterator<Item = (&'a str, &'a str, bool)>) -> Columns {
        Columns { items: items.into_iter().map(|(k, h, on)| (k.to_string(), h.to_string(), on)).collect(), ..Columns::default() }
    }

    /// Offer these columns (named by themselves): the ones already listed
    /// keep their place and tick, new ones join the end ticked.
    pub fn offer(&mut self, names: &[String]) {
        for (key, _, shown) in &self.items {
            self.remembered.insert(key.clone(), *shown);
        }
        let mut items: Vec<(String, String, bool)> = self.items.iter().filter(|(k, _, _)| names.contains(k)).cloned().collect();
        for name in names {
            if !items.iter().any(|(k, _, _)| k == name) {
                items.push((name.clone(), name.clone(), self.remembered.get(name).copied().unwrap_or(true)));
            }
        }
        if items.iter().map(|(k, _, _)| k).ne(self.items.iter().map(|(k, _, _)| k)) {
            self.selected = None;
        }
        self.items = items;
    }

    /// The ticked columns' keys, in order.
    pub fn chosen(&self) -> Vec<String> {
        self.items.iter().filter(|(_, _, on)| *on).map(|(k, _, _)| k.clone()).collect()
    }

    fn shift(&mut self, by: isize) {
        let Some(at) = self.selected else { return };
        let to = at as isize + by;
        if to < 0 || to as usize >= self.items.len() {
            return;
        }
        let item = self.items.remove(at);
        self.items.insert(to as usize, item);
        self.selected = Some(to as usize);
    }

    pub fn show(&mut self, ui: &mut egui::Ui, id: &str, height: f32) {
        let p = theme::palette(ui.ctx());
        ui.horizontal_top(|ui| {
            egui::Frame::new().stroke(egui::Stroke::new(1.0, p.border)).corner_radius(6).inner_margin(egui::Margin::same(4)).show(ui, |ui| {
                ui.set_width(280.0);
                egui::ScrollArea::vertical().id_salt(id).max_height(height).auto_shrink([false, true]).show(ui, |ui| {
                    ui.vertical(|ui| {
                        ui.spacing_mut().item_spacing.y = 2.0;
                        for i in 0..self.items.len() {
                            ui.horizontal(|ui| {
                                ui.checkbox(&mut self.items[i].2, "");
                                let selected = self.selected == Some(i);
                                if ui.selectable_label(selected, &self.items[i].1).clicked() {
                                    self.selected = if selected { None } else { Some(i) };
                                }
                            });
                        }
                    });
                });
            });
            ui.vertical(|ui| {
                ui.style_mut().wrap_mode = Some(egui::TextWrapMode::Extend);
                let (len, selected) = (self.items.len() as isize, self.selected);
                let can = |by: isize| selected.is_some_and(|at| (0..len).contains(&(at as isize + by)));
                if ui.add_enabled(can(-1), egui::Button::new("⏶ Up")).on_disabled_hover_text("Click a column's name to choose it").clicked() {
                    self.shift(-1);
                }
                if ui.add_enabled(can(1), egui::Button::new("⏷ Down")).on_disabled_hover_text("Click a column's name to choose it").clicked() {
                    self.shift(1);
                }
            });
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn names(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn offering_again_keeps_order_and_ticks() {
        let mut columns = Columns::named(&names(&["Name", "Jan-26", "Feb-26", "Total"]));
        columns.selected = Some(3);
        columns.shift(-3);
        columns.items[2].2 = false; // untick Jan-26
        assert_eq!(columns.chosen(), names(&["Total", "Name", "Feb-26"]));
        // Another quarter: the months change, the rest stays as arranged.
        columns.offer(&names(&["Name", "Feb-26", "Mar-26", "Total"]));
        assert_eq!(columns.chosen(), names(&["Total", "Name", "Feb-26", "Mar-26"]));
        // Jan-26 comes back unticked, as it was left.
        columns.offer(&names(&["Name", "Jan-26", "Total"]));
        assert_eq!(columns.chosen(), names(&["Total", "Name"]));
    }
}
