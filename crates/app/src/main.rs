//! LoanMaster: loan, savings and fund management for SACCOs.
//!
//! `loanmaster [journal.db] [--software]`
//!
//! `--software` (or `LOANMASTER_SOFTWARE_RENDERING=1`) draws without the
//! graphics card — WARP on Windows, llvmpipe elsewhere — for machines whose
//! drivers misbehave. If the window cannot start at all, LoanMaster relaunches
//! itself once that way.
#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::sync::Arc;

use eframe::{egui, egui_wgpu, wgpu};

mod app;
mod dialogs;
mod logging;
mod screens;
mod task;
mod theme;
mod widgets;
mod workspace;

#[cfg(test)]
mod tests;

/// The application icon, for the window and the screens that show it.
pub const ICON: &[u8] = include_bytes!("../../../resources/icon.png");
const RELAUNCHED: &str = "LOANMASTER_RELAUNCHED";

/// The graphics adapter to draw with: a real GPU when there is one that can
/// present to the window, otherwise — or when asked — a software one.
fn choose_adapter(adapters: &[wgpu::Adapter], surface: Option<&wgpu::Surface<'_>>, software: bool) -> Result<wgpu::Adapter, String> {
    let usable: Vec<&wgpu::Adapter> = adapters.iter().filter(|a| surface.is_none_or(|s| a.is_surface_supported(s))).collect();
    let find = |wanted: &dyn Fn(wgpu::DeviceType) -> bool| usable.iter().copied().find(|a| wanted(a.get_info().device_type));
    let chosen = if software {
        find(&|t| t == wgpu::DeviceType::Cpu)
    } else {
        find(&|t| matches!(t, wgpu::DeviceType::DiscreteGpu | wgpu::DeviceType::IntegratedGpu))
            .or_else(|| find(&|t| t != wgpu::DeviceType::Cpu))
            .or_else(|| usable.first().copied())
    };
    match chosen {
        Some(adapter) => {
            let info = adapter.get_info();
            log::info!("Drawing with {} ({:?}, {:?})", info.name, info.device_type, info.backend);
            Ok(adapter.clone())
        }
        None if software => Err("No software renderer is available on this machine.".into()),
        None => Err("No graphics adapter on this machine can draw the window.".into()),
    }
}

fn main() -> eframe::Result {
    let log_file = logging::init();
    logging::install_panic_hook();
    log::info!("LoanMaster {} starting; log at {}", env!("CARGO_PKG_VERSION"), log_file.display());

    let mut journal: Option<PathBuf> = None;
    let mut software = std::env::var_os("LOANMASTER_SOFTWARE_RENDERING").is_some();
    for arg in std::env::args_os().skip(1) {
        if arg == "--software" {
            software = true;
        } else if journal.is_none() {
            journal = Some(PathBuf::from(arg));
        }
    }

    let mut wgpu_options = egui_wgpu::WgpuConfiguration::default();
    if let egui_wgpu::WgpuSetup::CreateNew(setup) = &mut wgpu_options.wgpu_setup {
        setup.native_adapter_selector = Some(Arc::new(move |adapters, surface| choose_adapter(adapters, surface, software)));
    }
    let mut viewport = egui::ViewportBuilder::default()
        .with_title("LoanMaster")
        .with_app_id("loanmaster")
        .with_inner_size([1240.0, 780.0])
        .with_min_inner_size([960.0, 600.0]);
    match eframe::icon_data::from_png_bytes(ICON) {
        Ok(icon) => viewport = viewport.with_icon(Arc::new(icon)),
        Err(e) => log::warn!("The window icon could not be read: {e}"),
    }
    let options = eframe::NativeOptions { viewport, wgpu_options, ..Default::default() };

    let result = eframe::run_native(
        "LoanMaster",
        options,
        Box::new(move |cc| Ok(Box::new(app::LoanMasterApp::new(cc, journal)))),
    );
    if let Err(error) = &result {
        log::error!("The window could not start: {error}");
        if !software && std::env::var_os(RELAUNCHED).is_none() {
            log::warn!("Relaunching with software rendering");
            if let Ok(exe) = std::env::current_exe() {
                let relaunch =
                    std::process::Command::new(exe).args(std::env::args_os().skip(1)).arg("--software").env(RELAUNCHED, "1").spawn();
                if relaunch.is_ok() {
                    return Ok(());
                }
            }
        }
    }
    result
}
