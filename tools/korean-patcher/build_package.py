"""Build an offline package from the user's own translated masters."""
import argparse
import json
from pathlib import Path
import shutil

from patcher import write_json
from plugin_text import digest, extract

OFFICIAL = ["FalloutNV.esm", "DeadMoney.esm", "HonestHearts.esm", "OldWorldBlues.esm",
            "LonesomeRoad.esm", "GunRunnersArsenal.esm", "CaravanPack.esm", "ClassicPack.esm",
            "MercenaryPack.esm", "TribalPack.esm"]


def build(game, engine, fonts, output, source, build_name, launcher):
    if output.exists():
        raise ValueError("새 출력 폴더를 지정하세요")
    output.mkdir(parents=True)
    payload = output / "payload"
    payload.mkdir()
    for name in ("nvforever.exe", "LICENSE"):
        shutil.copy2(engine / name, payload / name)
    shutil.copy2(launcher, payload / "nvforever-launcher.exe")
    shutil.copytree(engine / "schemas", payload / "schemas")
    (payload / "lang").mkdir()
    shutil.copy2(engine / "lang/ko.toml", payload / "lang/ko.toml")
    (payload / "fonts").mkdir()
    for name in ("Pretendard-Bold.ttf", "Pretendard-LICENSE.txt"):
        shutil.copy2(fonts / name, payload / "fonts" / name)
    # GPL corresponding source is supplied locally alongside the package.
    shutil.copy2(source, output / "NVForever-corresponding-source.zip")
    (output / "translations").mkdir()
    plugins = []
    for name in OFFICIAL:
        path = game / "Data" / name
        if not path.exists():
            continue
        catalog = extract(path.read_bytes())
        write_json(output / "translations" / f"{name}.json", catalog)
        plugins.append(name)
        print(name, json.dumps(catalog["stats"]), flush=True)
    files = {str(path.relative_to(output)).replace("\\", "/"): digest(path.read_bytes())
             for path in output.rglob("*") if path.is_file()}
    write_json(output / "manifest.json", {"format": 1, "build": build_name,
                                          "plugins": plugins, "files": files})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("game", "engine", "fonts", "output", "source", "launcher"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--build", required=True)
    args = parser.parse_args()
    build(args.game, args.engine, args.fonts, args.output, args.source, args.build, args.launcher)
