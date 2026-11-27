//! NVForever's launcher (#2708, docs/design/launcher.md): the project's news, Play/Continue
//! and the settings of the home's `NVForever.ini` in one window, beside `nvforever.exe`.
//! It writes only NVForever's own files (the INI, `plugins.txt`), never the game folder or
//! the original game's (`fnv_install::guard_write`).

#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

mod app;
mod catalog;
mod display;
mod dlss;
mod fonts;
mod launch;
mod news;
mod reports;
mod start;
mod theme;
mod update;

const ICON: &[u8] = include_bytes!("../assets/icon-64.png");

fn main() -> eframe::Result {
    // Select the installed Korean profile before the UI creates worker threads.
    let directory = std::env::current_exe().ok().and_then(|p| p.parent().map(std::path::Path::to_path_buf));
    if let Some(home) = directory.as_ref().map(|d| d.join("korean-home")).filter(|h| h.join("NVForever.ini").is_file()) {
        std::env::set_var("NVFOREVER_HOME", home);
    }
    let home = fnv_install::EngineHome::user();
    fnv_util::words::load(&fnv_util::words::dirs(directory.as_deref(), home.as_ref().map(|h| h.root.as_path())));
    let ini = home.as_ref().and_then(|h| std::fs::read_to_string(h.config_file()).ok()).unwrap_or_default();
    if let Some(language) = fnv_install::ini_get(&ini, "Settings", "language") {
        fnv_util::words::set_language(&language);
    }
    let mut viewport = eframe::egui::ViewportBuilder::default()
        .with_title(fnv_util::words::tr("NVForever Launcher"))
        .with_inner_size([1180.0, 760.0])
        .with_min_inner_size([900.0, 560.0]);

    if let Ok(icon) = eframe::icon_data::from_png_bytes(ICON) {
        viewport = viewport.with_icon(icon);
    }
    let options = eframe::NativeOptions { viewport, ..Default::default() };
    eframe::run_native("NVForever Launcher", options, Box::new(|cc| Ok(Box::new(app::App::new(cc)))))
}
