//! Light and dark themes, with the palette LoanMaster 1.x used: slate
//! surfaces, a blue accent, and one colour per meaning (success, danger,
//! warning, info).

use eframe::egui::{self, Color32, CornerRadius, Stroke, Theme, Visuals};

/// Every colour the screens ask for, by meaning. The whole palette is kept
/// even where no screen uses a colour yet, so the two themes stay matched.
#[allow(dead_code)]
#[derive(Debug, Clone, Copy)]
pub struct Palette {
    pub background: Color32,
    pub card: Color32,
    pub text: Color32,
    pub muted: Color32,
    pub accent: Color32,
    pub border: Color32,
    pub input: Color32,
    pub selected: Color32,
    pub success: Color32,
    pub danger: Color32,
    pub warning: Color32,
    pub info: Color32,
    pub purple: Color32,
    pub neutral: Color32,
    pub danger_bg: Color32,
    pub warning_bg: Color32,
    pub success_bg: Color32,
    /// The band across the top of the dashboard.
    pub header: Color32,
}

const fn hex(rgb: u32) -> Color32 {
    Color32::from_rgb((rgb >> 16) as u8, (rgb >> 8) as u8, rgb as u8)
}

pub const LIGHT: Palette = Palette {
    background: hex(0xF3F4F6),
    card: hex(0xFFFFFF),
    text: hex(0x1F2937),
    muted: hex(0x4B5563),
    accent: hex(0x2563EB),
    border: hex(0xE5E7EB),
    input: hex(0xFFFFFF),
    selected: hex(0xEFF6FF),
    success: hex(0x059669),
    danger: hex(0xDC2626),
    warning: hex(0xD97706),
    info: hex(0x0284C7),
    purple: hex(0x7C3AED),
    neutral: hex(0x6B7280),
    danger_bg: hex(0xFEF2F2),
    warning_bg: hex(0xFFFBEB),
    success_bg: hex(0xECFDF5),
    header: hex(0x1E293B),
};

pub const DARK: Palette = Palette {
    background: hex(0x111827),
    card: hex(0x1F2937),
    text: hex(0xF9FAFB),
    muted: hex(0x9CA3AF),
    accent: hex(0x60A5FA),
    border: hex(0x374151),
    input: hex(0x374151),
    selected: hex(0x1E293B),
    success: hex(0x34D399),
    danger: hex(0xF87171),
    warning: hex(0xFBBF24),
    info: hex(0x38BDF8),
    purple: hex(0xA78BFA),
    neutral: hex(0x4B5563),
    danger_bg: hex(0x3F1D1D),
    warning_bg: hex(0x451A03),
    success_bg: hex(0x022C22),
    header: hex(0x0F172A),
};

/// The palette of the theme being shown.
pub fn palette(ctx: &egui::Context) -> &'static Palette {
    if ctx.theme() == Theme::Dark { &DARK } else { &LIGHT }
}

/// Text that reads on a solid background of `fill`.
pub fn text_on(fill: Color32) -> Color32 {
    let luminance = 0.299 * fill.r() as f32 + 0.587 * fill.g() as f32 + 0.114 * fill.b() as f32;
    if luminance > 160.0 { hex(0x1F2937) } else { Color32::WHITE }
}

fn visuals(p: &Palette, dark: bool) -> Visuals {
    let mut v = if dark { Visuals::dark() } else { Visuals::light() };
    v.panel_fill = p.background;
    v.window_fill = p.card;
    v.window_stroke = Stroke::new(1.0, p.border);
    v.window_corner_radius = CornerRadius::same(10);
    v.menu_corner_radius = CornerRadius::same(6);
    v.extreme_bg_color = p.input;
    v.text_edit_bg_color = Some(p.input);
    v.faint_bg_color = if dark { hex(0x243042) } else { hex(0xF8FAFC) };
    v.code_bg_color = p.background;
    v.hyperlink_color = p.accent;
    v.warn_fg_color = p.warning;
    v.error_fg_color = p.danger;
    v.selection.bg_fill = p.accent.gamma_multiply(if dark { 0.45 } else { 0.25 });
    v.selection.stroke = Stroke::new(1.0, p.accent);
    for (state, fill) in [
        (&mut v.widgets.inactive, if dark { hex(0x2B3646) } else { hex(0xF1F5F9) }),
        (&mut v.widgets.hovered, if dark { hex(0x334155) } else { hex(0xE2E8F0) }),
        (&mut v.widgets.active, if dark { hex(0x475569) } else { hex(0xCBD5E1) }),
    ] {
        state.bg_fill = fill;
        state.weak_bg_fill = fill;
        state.corner_radius = CornerRadius::same(6);
    }
    v.widgets.noninteractive.bg_stroke = Stroke::new(1.0, p.border);
    v.widgets.noninteractive.fg_stroke = Stroke::new(1.0, p.text);
    v.widgets.inactive.fg_stroke = Stroke::new(1.0, p.text);
    v.widgets.hovered.bg_stroke = Stroke::new(1.0, p.accent);
    v.widgets.noninteractive.corner_radius = CornerRadius::same(6);
    v
}

/// Install both themes and show `dark` or light.
pub fn apply(ctx: &egui::Context, dark: bool) {
    ctx.set_visuals_of(Theme::Light, visuals(&LIGHT, false));
    ctx.set_visuals_of(Theme::Dark, visuals(&DARK, true));
    for theme in [Theme::Light, Theme::Dark] {
        ctx.style_mut_of(theme, |style| {
            style.spacing.item_spacing = egui::vec2(8.0, 6.0);
            style.spacing.button_padding = egui::vec2(10.0, 4.0);
            style.spacing.interact_size.y = 24.0;
            style.text_styles.insert(egui::TextStyle::Heading, egui::FontId::proportional(20.0));
            style.text_styles.insert(egui::TextStyle::Body, egui::FontId::proportional(14.0));
            style.text_styles.insert(egui::TextStyle::Button, egui::FontId::proportional(14.0));
            style.text_styles.insert(egui::TextStyle::Small, egui::FontId::proportional(11.0));
        });
    }
    ctx.set_theme(if dark { Theme::Dark } else { Theme::Light });
}
