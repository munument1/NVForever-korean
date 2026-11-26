"""Read the real translated masters, then install only into a disposable copy."""
import copy
import argparse
from pathlib import Path
import tempfile
from unittest.mock import patch

from patcher import install_package, read_json, restore, verify_package, write_json
from plugin_text import digest, extract, translate


def verify(package, game):
    manifest = verify_package(package)
    total = 0
    for name in manifest["plugins"]:
        original = (game / "Data" / name).read_bytes()
        catalog = read_json(package / "translations" / f"{name}.json")
        result, stats = translate(original, catalog)
        assert result == original, (name, stats)
        assert stats["changed"] == stats["missing"] == stats["incompatible"] == 0
        total += stats["already"]
        print(name, stats, flush=True)
    with tempfile.TemporaryDirectory(prefix="nvf-kr-verify-") as temporary:
        root = Path(temporary)
        install = root / "NVForever"
        sandbox_game = root / "Game"
        install.mkdir()
        (sandbox_game / "Data").mkdir(parents=True)
        (install / "nvforever.exe").write_bytes(b"synthetic original executable")
        catalog = read_json(package / "translations/FalloutNV.esm.json")
        english_catalog = copy.deepcopy(catalog)
        for entry in english_catalog["records"].values():
            for value in entry["values"].values():
                value["text"] = "English (123)"
        original = (game / "Data/FalloutNV.esm").read_bytes()
        english, _ = translate(original, english_catalog)
        target = sandbox_game / "Data/FalloutNV.esm"
        target.write_bytes(english)
        before = digest(english)
        with patch("patcher.launcher_home", return_value=root / "launcher-home"):
            install_package(install, sandbox_game, package)
        applied = target.read_bytes()
        assert extract(applied)["records"] == catalog["records"]
        restore(install, sandbox_game)
        assert digest(target.read_bytes()) == before
        assert (install / "nvforever.exe").read_bytes() == b"synthetic original executable"
    print(f"PASS {len(manifest['plugins'])} plugins / {total} fields; full-size synthetic-English install and restore", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--game", type=Path, required=True)
    args = parser.parse_args()
    verify(args.package, args.game)
