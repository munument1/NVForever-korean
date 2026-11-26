"""Rebuild the Windows Korean patcher with the exact package from a prior release."""
import argparse
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from PyInstaller.archive.readers import CArchiveReader
from patcher import digest, verify_package


def extract_package(executable, destination):
    destination.mkdir(parents=True, exist_ok=False)
    reader = CArchiveReader(str(executable))
    seen = set()
    for name in reader.toc:
        parts = PurePosixPath(name.replace("\\", "/")).parts
        if not parts or parts[0] != "package":
            continue
        relative = parts[1:]
        if not relative or any(part in {"..", "."} or ":" in part for part in relative):
            raise ValueError(f"Unsafe package path: {name}")
        path = destination.joinpath(*relative)
        if path in seen or not path.resolve().is_relative_to(destination.resolve()):
            raise ValueError(f"Duplicate or unsafe package path: {name}")
        seen.add(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(reader.extract(name))
    verify_package(destination)


def hashes(root):
    return {str(path.relative_to(root)): digest(path.read_bytes())
            for path in root.rglob("*") if path.is_file()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("old_exe", type=Path)
    parser.add_argument("checksums", type=Path)
    parser.add_argument("package", type=Path)
    parser.add_argument("--rebuilt", type=Path)
    args = parser.parse_args()
    entries = {}
    for line in args.checksums.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            value, name = line.split(maxsplit=1)
            entries[name.lstrip("*")] = value.lower()
    if digest(args.old_exe.read_bytes()) != entries[args.old_exe.name]:
        raise ValueError("Prior installer checksum mismatch")
    if args.rebuilt is None:
        extract_package(args.old_exe, args.package)
    else:
        with TemporaryDirectory() as temp:
            rebuilt_package = Path(temp) / "package"
            extract_package(args.rebuilt, rebuilt_package)
            if hashes(args.package) != hashes(rebuilt_package):
                raise ValueError("Rebuilt installer package differs from the verified original")
    print("Embedded package verified.")


if __name__ == "__main__":
    main()
