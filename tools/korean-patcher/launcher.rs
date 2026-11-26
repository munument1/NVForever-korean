#![windows_subsystem = "windows"]

use std::{env, io, process::Command};

fn launch() -> io::Result<()> {
    let executable = env::current_exe()?;
    let folder = executable
        .parent()
        .ok_or_else(|| io::Error::other("Unknown install folder"))?;
    let home = folder.join("korean-home");
    if !home.join("NVForever.ini").is_file() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            "Apply the Korean patch first",
        ));
    }
    Command::new(folder.join("nvforever-launcher-original.exe"))
        .args(env::args_os().skip(1))
        .current_dir(folder)
        .env("NVFOREVER_HOME", home)
        .env("NVF_CODE_PAGE", "65001")
        .env_remove("NVF_LANG_DIR")
        .spawn()?;
    Ok(())
}

fn main() {
    if let Err(error) = launch() {
        let text: Vec<u16> = format!("Cannot start NVForever Korean launcher.\n{error}\0")
            .encode_utf16()
            .collect();
        let title: Vec<u16> = "NVForever Korean Launcher\0".encode_utf16().collect();
        #[link(name = "user32")]
        unsafe extern "system" {
            fn MessageBoxW(
                window: *mut core::ffi::c_void,
                text: *const u16,
                title: *const u16,
                kind: u32,
            ) -> i32;
        }
        unsafe {
            MessageBoxW(core::ptr::null_mut(), text.as_ptr(), title.as_ptr(), 0x10);
        }
        std::process::exit(1);
    }
}
