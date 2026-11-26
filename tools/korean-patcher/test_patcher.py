import json
from pathlib import Path
import struct
import tempfile
import unittest
import sys
from unittest.mock import patch
import zlib

import patcher
from plugin_text import digest, extract, translate, transform


def field(tag, value, extended=False):
    if extended or len(value) > 65535:
        return b"XXXX\x04\0" + struct.pack("<I", len(value)) + tag + b"\0\0" + value
    return tag + struct.pack("<H", len(value)) + value


def record(tag, form, payload, compressed=False):
    if compressed:
        payload = struct.pack("<I", len(payload)) + zlib.compress(payload)
    return struct.pack("<4sIIIIHH", tag, len(payload), 0x40000 if compressed else 0, form, 0, 15, 0) + payload


def plugin(text, compressed=False, extra=b"", extended=False):
    item = record(b"QUST", 123, field(b"EDID", b"TestQuest\0") +
                  field(b"FULL", text.encode() + b"\0", extended) +
                  field(b"CNAM", b"first\0") + field(b"CNAM", b"second\0") +
                  field(b"DATA", b"\xff\x10") + extra, compressed)
    group = b"GRUP" + struct.pack("<I", len(item) + 24) + b"QUST" + bytes(12) + item
    return record(b"TES4", 0, b"") + group


class TextTests(unittest.TestCase):
    def test_shipped_zlib_trailer_anomaly_and_truncation(self):
        data = record(b"TES4", 0, b"") + record(b"WEAP", 9, field(b"FULL", b"Name\0"), True)
        wrong = data[:-1] + bytes([data[-1] ^ 1])
        with self.assertWarns(UserWarning):
            self.assertEqual(extract(wrong)["stats"]["fields"], 1)
        with self.assertRaises(ValueError):
            # Keep record size valid while truncating the deflate body itself.
            truncated = data[:-9]
            truncated = truncated[:28] + struct.pack("<I", len(truncated) - 48) + truncated[32:]
            extract(truncated)

    def test_roundtrip_compression_repeated_and_extended_fields(self):
        for compressed in (False, True):
            for extended in (False, True):
                english = plugin("Test (123)", compressed, extended=extended)
                korean = plugin("퀘스트(123) 漢字 カナ", compressed, extended=extended)
                catalog = extract(korean)
                result, stats = translate(english, catalog)
                self.assertEqual(extract(result)["records"], catalog["records"])
                self.assertEqual(stats["changed"], 1)
                self.assertEqual(stats["already"], 2)
                unchanged, stats = translate(result, catalog)
                self.assertEqual(unchanged, result)
                self.assertEqual(stats["changed"], 0)

    def test_growth_to_extended_field(self):
        result, stats = translate(plugin("a"), extract(plugin("한" * 30000)))
        self.assertEqual(stats["changed"], 1)
        self.assertIn(b"XXXX", result)
        self.assertEqual(extract(result)["stats"]["fields"], 3)

    def test_nontext_change_is_rejected(self):
        result, stats = translate(plugin("English", extra=field(b"INTV", b"\1")), extract(plugin("한글")))
        self.assertEqual(result, plugin("English", extra=field(b"INTV", b"\1")))
        self.assertEqual(stats["incompatible"], 1)

    def test_invalid_utf8_is_skipped_and_truncation_rejected(self):
        bad = record(b"TES4", 0, b"") + record(b"WEAP", 2, field(b"FULL", b"\xff\0"))
        self.assertEqual(extract(bad)["stats"]["invalid_utf8"], 1)
        with self.assertRaises(ValueError):
            extract(bad[:-1])

    def test_conditional_note_and_game_setting(self):
        for tag, fields, expected in [
            (b"NOTE", field(b"DATA", b"\1") + field(b"TNAM", "노트\0".encode()), 1),
            (b"NOTE", field(b"DATA", b"\3") + field(b"TNAM", b"abcd"), 0),
            (b"GMST", field(b"EDID", b"sHello\0") + field(b"DATA", "안녕\0".encode()), 1),
            (b"GMST", field(b"EDID", b"iHello\0") + field(b"DATA", b"abcd"), 0)]:
            data = record(b"TES4", 0, b"") + record(tag, 42, fields)
            self.assertEqual(extract(data)["stats"]["fields"], expected)


class InstallTests(unittest.TestCase):
    def test_embedded_package_uses_extracted_resources(self):
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", "C:/temporary/installer", create=True):
            self.assertEqual(patcher.package_directory(), Path("C:/temporary/installer/package"))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.home = base / "설치/korean-home"
        home_patch = patch.object(patcher, "launcher_home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.install, self.game, self.package = [base / name for name in ("설치", "게임", "패키지")]
        (self.game / "Data").mkdir(parents=True)
        self.install.mkdir()
        (self.install / "nvforever.exe").write_bytes(b"original")
        (self.game / "Data/FalloutNV.esm").write_bytes(plugin("English"))
        (self.package / "payload/fonts").mkdir(parents=True)
        (self.package / "payload/nvforever.exe").write_bytes(b"patched")
        (self.package / "payload/fonts/Pretendard-Bold.ttf").write_bytes(b"font")
        (self.package / "translations").mkdir()
        patcher.write_json(self.package / "translations/FalloutNV.esm.json", extract(plugin("한글")))
        files = {str(p.relative_to(self.package)).replace("\\", "/"): digest(p.read_bytes())
                 for p in self.package.rglob("*") if p.is_file()}
        patcher.write_json(self.package / "manifest.json", {"format": 1, "build": "test",
                            "plugins": ["FalloutNV.esm"], "files": files})

    def install_patch(self):
        patcher.install_package(self.install, self.game, self.package, log=lambda text: None)

    def test_install_restore_and_repeat(self):
        plugin_path = self.game / "Data/FalloutNV.esm"
        original_mtime = plugin_path.stat().st_mtime_ns
        for _ in range(2):
            self.install_patch()
            self.assertEqual(plugin_path.stat().st_mtime_ns, original_mtime)
            self.assertEqual((self.install / "nvforever.exe").read_bytes(), b"patched")
            self.assertEqual(extract((self.game / "Data/FalloutNV.esm").read_bytes())["records"], extract(plugin("한글"))["records"])
            patcher.restore(self.install, self.game, lambda text: None)
            self.assertEqual((self.install / "nvforever.exe").read_bytes(), b"original")
            self.assertEqual((self.game / "Data/FalloutNV.esm").read_bytes(), plugin("English"))
            self.assertEqual(plugin_path.stat().st_mtime_ns, original_mtime)

    def test_partial_translation_skips_incompatible_and_missing_records(self):
        prefix = record(b"TES4", 0, b"")

        def named(form, text, extra=b"", compressed=False):
            return record(b"WEAP", form, field(b"FULL", text.encode("utf-8") + b"\0") + extra, compressed)

        already = named(2, "이미 번역")
        incompatible = named(3, "Keep original", field(b"DATA", b"\x01"), True)
        original = prefix + named(1, "English") + already + incompatible
        translated = prefix + named(1, "한국어") + already + named(3, "적용 금지") + named(4, "누락")
        expected = prefix + named(1, "한국어") + already + incompatible
        plugin_path = self.game / "Data/FalloutNV.esm"
        plugin_path.write_bytes(original)
        original_mtime = plugin_path.stat().st_mtime_ns
        relative = "translations/FalloutNV.esm.json"
        catalog_path = self.package / relative
        patcher.write_json(catalog_path, extract(translated))
        manifest = patcher.read_json(self.package / "manifest.json")
        manifest["files"][relative] = digest(catalog_path.read_bytes())
        patcher.write_json(self.package / "manifest.json", manifest)

        messages = []
        patcher.install_package(self.install, self.game, self.package, log=messages.append)
        self.assertEqual(plugin_path.read_bytes(), expected)
        self.assertEqual(plugin_path.stat().st_mtime_ns, original_mtime)
        self.assertEqual((self.install / "nvforever.exe").read_bytes(), b"patched")
        journal = patcher.read_json(self.install / patcher.STATE)
        self.assertEqual(journal["status"], "installed")
        self.assertEqual(journal["summary"], [{"plugin": "FalloutNV.esm", "changed": 1,
                                                "already": 1, "incompatible": 1, "missing": 1}])
        self.assertTrue(any("구조 불일치 레코드 1개" in message and "누락 레코드 1개" in message for message in messages))
        self.assertTrue(any("레코드 2개를 건너뛰었습니다" in message for message in messages))
        self.assertTrue(any("설치 완료" in message for message in messages))
        patcher.restore(self.install, self.game, lambda message: None)
        self.assertEqual(plugin_path.read_bytes(), original)
        self.assertEqual(plugin_path.stat().st_mtime_ns, original_mtime)
        self.assertEqual((self.install / "nvforever.exe").read_bytes(), b"original")
        self.assertFalse((self.install / patcher.STATE).exists())

    def test_malformed_esm_still_aborts_install(self):
        plugin_path = self.game / "Data/FalloutNV.esm"
        malformed = plugin("English")[:-1]
        plugin_path.write_bytes(malformed)
        with self.assertRaises(ValueError):
            self.install_patch()
        self.assertEqual(plugin_path.read_bytes(), malformed)
        self.assertEqual((self.install / "nvforever.exe").read_bytes(), b"original")
        self.assertFalse((self.install / patcher.STATE).exists())

    def test_package_tampering_changes_nothing(self):
        (self.package / "payload/nvforever.exe").write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            self.install_patch()
        self.assertEqual((self.install / "nvforever.exe").read_bytes(), b"original")

    def test_original_launcher_entry_and_restore(self):
        launcher = self.install / "nvforever-launcher.exe"
        launcher.write_bytes(b"official launcher")
        payload = self.package / "payload/nvforever-launcher.exe"
        payload.write_bytes(b"launcher bridge")
        manifest = patcher.read_json(self.package / "manifest.json")
        manifest["files"]["payload/nvforever-launcher.exe"] = digest(payload.read_bytes())
        patcher.write_json(self.package / "manifest.json", manifest)
        self.install_patch()
        self.assertEqual(launcher.read_bytes(), b"launcher bridge")
        self.assertEqual((self.install / "nvforever-launcher-original.exe").read_bytes(), b"official launcher")
        with patch.object(patcher.subprocess, "Popen") as spawn:
            patcher.play(self.install, self.game)
        self.assertEqual(spawn.call_args.args[0], [str(launcher)])
        self.assertEqual(spawn.call_args.kwargs["env"]["NVFOREVER_HOME"], str(self.home))
        patcher.restore(self.install, self.game, lambda message: None)
        self.assertEqual(launcher.read_bytes(), b"official launcher")
        self.assertFalse((self.install / "nvforever-launcher-original.exe").exists())

    def test_launcher_config_created_and_existing_settings_restored(self):
        self.home.mkdir(parents=True)
        original = b"[Controls]\nForward=Up\n[Settings]\nlanguage=en\n"
        config = self.home / "NVForever.ini"
        config.write_bytes(original)
        self.install_patch()
        text = config.read_text(encoding="utf-8")
        self.assertIn("Forward = Up", text)
        self.assertIn("language = ko", text)
        self.assertIn(str(self.game), text)
        patcher.restore(self.install, self.game, lambda message: None)
        self.assertEqual(config.read_bytes(), original)

    def test_partial_failure_rolls_back(self):
        original = patcher.atomic_write
        failed = False

        def write(path, data):
            nonlocal failed
            if path == self.game / "Data/FalloutNV.esm" and not failed:
                failed = True
                raise OSError("simulated write failure")
            original(path, data)
        with patch.object(patcher, "atomic_write", write):
            with self.assertRaises(OSError):
                self.install_patch()
        self.assertEqual((self.install / "nvforever.exe").read_bytes(), b"original")
        self.assertEqual((self.game / "Data/FalloutNV.esm").read_bytes(), plugin("English"))
        self.assertFalse((self.install / patcher.STATE).exists())

    def test_new_official_exe_is_preserved_on_restore(self):
        self.install_patch()
        (self.install / "nvforever.exe").write_bytes(b"new official version")
        with self.assertRaises(ValueError):
            patcher.restore(self.install, self.game)
        self.assertEqual((self.install / "nvforever.exe").read_bytes(), b"new official version")

    def test_path_traversal(self):
        for path in ("../outside", "C:/outside", "/outside", "a\\..\\outside"):
            with self.assertRaises(ValueError):
                patcher.child(self.install, path)


if __name__ == "__main__":
    unittest.main()
