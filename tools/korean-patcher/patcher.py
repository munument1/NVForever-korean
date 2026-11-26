"""Offline Windows installer for a specific NVForever Korean build."""
import argparse
import configparser
import json
import os
from pathlib import Path, PurePosixPath
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid

from plugin_text import digest, translate

STATE = ".nvf-kr-patch.json"


def package_directory():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "package"
    return Path(__file__).resolve().parent / "dist/NVForever-Korean-Patcher/package"


def launcher_home(install):
    return install / "korean-home"


def configured_ini(path, install, game):
    import io
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.optionxform = str
    if path.exists():
        config.read(path, encoding="utf-8-sig")
    for section, key, value in [("Text", "CodePage", "65001"),
                                ("Interface", "CjkFontPath", str(install / "fonts/Pretendard-Bold.ttf")),
                                ("Settings", "language", "ko"), ("Game", "Path", str(game))]:
        if not config.has_section(section):
            config.add_section(section)
        for old_key in list(config[section]):
            if old_key.lower() == key.lower():
                config.remove_option(section, old_key)
        config.set(section, key, value)
    text = io.StringIO()
    config.write(text)
    return text.getvalue().encode("utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def child(root, relative):
    parts = PurePosixPath(relative).parts
    if not parts or any(part in {"..", "."} or ":" in part or "\\" in part for part in parts):
        raise ValueError(f"잘못된 상대 경로 {relative}")
    path = root.joinpath(*parts).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError(f"폴더 밖의 경로 {relative}")
    return path


def verify_package(package):
    manifest = read_json(package / "manifest.json")
    if manifest.get("format") != 1:
        raise ValueError("지원하지 않는 패키지 형식입니다")
    for relative, expected in manifest["files"].items():
        if digest(child(package, relative).read_bytes()) != expected:
            raise ValueError(f"패키지 손상 {relative}")
    return manifest


def restore(install, game, log=print):
    journal_path = install / STATE
    journal = read_json(journal_path)
    if Path(journal["game"]).resolve() != game.resolve():
        raise ValueError("패치 당시 지정한 게임 폴더를 선택하세요")
    roots = {"install": install, "game": game, "home": Path(journal.get("home", install / "korean-home"))}
    backup = child(install, journal["backup"])
    # Preflight all backups and destinations before touching any file.
    for entry in journal["entries"]:
        target = child(roots[entry["root"]], entry["path"])
        current = digest(target.read_bytes()) if target.exists() else None
        if current not in {entry["before"], entry["after"]}:
            raise ValueError(f"패치 후 변경된 파일입니다. 별도로 보관한 후 복원하세요 {target}")
        if entry["before"] is not None:
            saved = child(backup, entry["saved"])
            if digest(saved.read_bytes()) != entry["before"]:
                raise ValueError(f"백업 손상 {saved}")
    for entry in reversed(journal["entries"]):
        target = child(roots[entry["root"]], entry["path"])
        if entry["before"] is None:
            target.unlink(missing_ok=True)
        else:
            saved = child(backup, entry["saved"])
            atomic_write(target, saved.read_bytes())
            shutil.copystat(saved, target)
    journal_path.unlink()
    log("복원 완료. 백업 폴더는 보관했습니다.")


def install_package(install, game, package, apply_text=True, log=print):
    install, game, package = install.resolve(), game.resolve(), package.resolve()
    if not (install / "nvforever.exe").is_file():
        raise ValueError("nvforever.exe가 있는 설치 폴더를 선택하세요")
    if not (game / "Data/FalloutNV.esm").is_file():
        raise ValueError("Data/FalloutNV.esm이 있는 게임 폴더를 선택하세요")
    if (install / STATE).exists():
        raise ValueError("기존 패치 기록이 있습니다. 복원 후 다시 적용하세요")
    manifest = verify_package(package)
    if "payload/nvforever.exe" not in manifest["files"] or "payload/fonts/Pretendard-Bold.ttf" not in manifest["files"]:
        raise ValueError("실행 파일 또는 한국어 폰트가 없는 패키지입니다")
    backup_name = f".nvf-kr-backups/{uuid.uuid4().hex}"
    backup = child(install, backup_name)
    pending = []
    destinations = set()
    home = launcher_home(install)
    if home.is_relative_to(game) or game.is_relative_to(home):
        raise ValueError("NVForever 설정 폴더와 원본 게임 폴더가 겹칩니다")
    roots = {"install": install, "game": game, "home": home}

    def stage(root_name, relative, data):
        root = roots[root_name]
        target = child(root, relative)
        if target in destinations:
            raise ValueError(f"중복 설치 대상 {target}")
        destinations.add(target)
        old = target.read_bytes() if target.exists() else None
        if old == data:
            return
        number = str(len(pending))
        if old is not None:
            atomic_write(backup / number, old)
            shutil.copystat(target, backup / number)
        atomic_write(backup / f"{number}.new", data)
        pending.append({"root": root_name, "path": relative, "saved": number,
                        "before": digest(old) if old is not None else None,
                        "after": digest(data)})

    for relative in manifest["files"]:
        if relative.startswith("payload/"):
            name = relative.removeprefix("payload/")
            if not (name in {"nvforever.exe", "nvforever-launcher.exe", "LICENSE", "lang/ko.toml", "fonts/Pretendard-Bold.ttf", "fonts/Pretendard-LICENSE.txt"}
                    or name.startswith("schemas/")):
                raise ValueError(f"허용되지 않은 설치 파일 {name}")
            if name == "nvforever-launcher.exe":
                original_launcher = install / name
                if not original_launcher.exists():
                    continue
                if (install / "nvforever-launcher-original.exe").exists():
                    raise ValueError("원본 런처 보관 파일이 이미 있습니다. 기존 패치를 복원한 후 다시 적용하세요")
                stage("install", "nvforever-launcher-original.exe", original_launcher.read_bytes())
            stage("install", relative.removeprefix("payload/"), child(package, relative).read_bytes())
    summary = []
    if apply_text:
        for name in manifest["plugins"]:
            target = child(game, f"Data/{name}")
            if not target.exists():
                log(f"미설치 DLC 건너뜀 {name}")
                continue
            catalog_path = f"translations/{name}.json"
            if catalog_path not in manifest["files"]:
                raise ValueError(f"검증 목록에 없는 번역 {name}")
            catalog = read_json(child(package, catalog_path))
            if catalog["stats"].get("invalid_utf8"):
                log(f"{name}  원본에서 UTF-8이 아닌 필드 {catalog['stats']['invalid_utf8']}개는 번역 목록에서 제외되었습니다")
            result, stats = translate(target.read_bytes(), catalog)
            log(f"{name}  변경 {stats['changed']} / 동일 {stats['already']} / 구조 불일치 {stats['incompatible']} / 없음 {stats['missing']}")
            if stats["incompatible"] or stats["missing"]:
                log(f"경고  {name}: 구조 불일치 레코드 {stats['incompatible']}개는 원본 유지, "
                    f"누락 레코드 {stats['missing']}개는 건너뜁니다. 일치하는 문자열만 적용합니다.")
            stage("game", f"Data/{name}", result)
            summary.append({"plugin": name, **stats})
    stage("home", "NVForever.ini", configured_ini(home / "NVForever.ini", install, game))
    journal = {"game": str(game), "home": str(home), "backup": backup_name, "entries": pending,
               "build": manifest["build"], "summary": summary, "status": "applying",
               "engine_sha256": manifest["files"]["payload/nvforever.exe"]}
    journal_path = install / STATE
    write_json(journal_path, journal)
    try:
        for entry in pending:
            root = roots[entry["root"]]
            target = child(root, entry["path"])
            # Reject a concurrent update between preparation and replacement.
            current = digest(target.read_bytes()) if target.exists() else None
            if current != entry["before"]:
                raise ValueError(f"설치 중 파일이 변경되었습니다 {target}")
            atomic_write(target, (backup / f"{entry['saved']}.new").read_bytes())
            if entry["root"] == "game" and entry["before"] is not None:
                # FNV installations can order plugins by modification time.
                shutil.copystat(backup / entry["saved"], target)
        journal["status"] = "installed"
        write_json(journal_path, journal)
    except BaseException:
        log("설치 실패. 원본 복원을 시도합니다.")
        restore(install, game, log)
        raise
    skipped = sum(item["incompatible"] + item["missing"] for item in summary)
    if skipped:
        log(f"주의  구조 불일치 또는 누락 레코드 {skipped}개를 건너뛰었습니다. 일부 번역이 적용되지 않았습니다.")
    log(f"설치 완료. 적용 빌드 {manifest['build']}. 아래 실행 버튼으로 시작하세요.")


def play(install, game):
    if not (install / STATE).is_file():
        raise ValueError("한국어 패치를 먼저 적용하세요")
    journal = read_json(install / STATE)
    if journal["status"] != "installed":
        raise ValueError("설치가 중단되었습니다. 복원 후 다시 적용하세요")
    if digest((install / "nvforever.exe").read_bytes()) != journal["engine_sha256"]:
        raise ValueError("실행 파일이 다른 버전으로 바뀌었습니다")
    home = Path(journal.get("runtime_home", journal.get("home", install / "korean-home")))
    env = dict(os.environ, NVFOREVER_HOME=str(home), NVF_CODE_PAGE="65001")
    env.pop("NVF_LANG_DIR", None)
    launcher = install / "nvforever-launcher.exe"
    if launcher.is_file():
        subprocess.Popen([str(launcher)], cwd=install, env=env)
    else:
        subprocess.Popen([str(install / "nvforever.exe"), "--data", str(game / "Data")],
                         cwd=install, env=env)


def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    root = tk.Tk()
    root.title("NVForever 한국어 패쳐")
    root.geometry("800x570")
    package = package_directory()
    frame = ttk.Frame(root, padding=18)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="NVForever 한국어 지원 설치", font=("맑은 고딕", 17, "bold")).pack(anchor="w")
    ttk.Label(frame, text="패키지에 포함된 특정 빌드로 실행 파일을 교체합니다. 새 공식 버전에는 해당 버전용 패키지가 필요합니다.", wraplength=750).pack(anchor="w", pady=8)
    variables = []
    for label, default in [("NVForever 설치 폴더", ""),
                           ("Fallout New Vegas 폴더", r"C:\Games\Steam\steamapps\common\Fallout New Vegas")]:
        ttk.Label(frame, text=label).pack(anchor="w", pady=(6, 0))
        row = ttk.Frame(frame)
        row.pack(fill="x")
        variable = tk.StringVar(value=default)
        variables.append(variable)
        ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True)
        def browse(value=variable):
            selected = filedialog.askdirectory()
            if selected:
                value.set(selected)
        ttk.Button(row, text="찾기", command=browse).pack(side="right", padx=(6, 0))
    apply_text = tk.BooleanVar(value=True)
    ttk.Checkbutton(frame, text="본편·설치된 DLC의 ESM에 추출한 UTF-8 번역 적용", variable=apply_text).pack(anchor="w", pady=10)
    ttk.Label(frame, text="설정·세이브는 설치 폴더의 korean-home을 사용합니다. 설치 후 기존 nvforever-launcher.exe를 열고 Play를 누르세요.", wraplength=750).pack(anchor="w")
    row = ttk.Frame(frame)
    row.pack(fill="x", pady=10)
    output = tk.Text(frame, height=10, state="disabled", wrap="word")
    output.pack(fill="both", expand=True)
    messages = queue.Queue()
    buttons = []

    def append(message):
        output.configure(state="normal")
        output.insert("end", message + "\n")
        output.see("end")
        output.configure(state="disabled")

    def action(kind):
        if any(not value.get().strip() for value in variables):
            messagebox.showerror("경로 확인", "설치 폴더와 게임 폴더를 지정하세요")
            return
        install, game = [Path(value.get()) for value in variables]
        if kind == "install":
            try:
                manifest = verify_package(package)
            except Exception as error:
                messagebox.showerror("패키지 확인", str(error))
                return
            if not messagebox.askyesno("설치 확인", f"적용 빌드 {manifest['build']}\n\n실행 중인 게임을 종료하세요. 실행 파일과 선택한 ESM을 백업하고 교체합니다.\n계속할까요?"):
                return
        text_enabled = apply_text.get()
        for button in buttons:
            button.configure(state="disabled")

        def worker():
            try:
                if kind == "install":
                    install_package(install, game, package, text_enabled, messages.put)
                elif kind == "restore":
                    restore(install, game, messages.put)
                else:
                    play(install, game)
            except Exception as error:
                messages.put(f"오류  {error}")
            finally:
                messages.put(None)
        threading.Thread(target=worker, daemon=False).start()

    for label, kind in [("패치 적용", "install"), ("원본 복원", "restore"), ("한국어로 실행", "play")]:
        button = ttk.Button(row, text=label, command=lambda k=kind: action(k))
        button.pack(side="left", padx=(0, 8))
        buttons.append(button)

    def poll():
        while not messages.empty():
            value = messages.get()
            if value is None:
                for button in buttons:
                    button.configure(state="normal")
            else:
                append(value)
        root.after(100, poll)
    root.protocol("WM_DELETE_WINDOW", lambda: root.destroy() if all(str(b["state"]) != "disabled" for b in buttons) else messagebox.showinfo("작업 중", "파일 작업이 끝날 때까지 기다려 주세요"))
    poll()
    root.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", type=Path, nargs="?", const=package_directory())
    args = parser.parse_args()
    if args.verify:
        print(verify_package(args.verify)["build"])
    else:
        gui()
