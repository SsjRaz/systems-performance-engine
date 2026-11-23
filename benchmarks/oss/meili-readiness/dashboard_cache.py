"""Populate the upstream mini-dashboard build cache from the verified release archive."""

import argparse
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile

SHA1 = "c4de9bfd4bd4ddb87a62cbe00a4150bdf0b6f9d1"
SHA256 = "75f1f8b4fee7ff60179f519b853fa2f0cef347df95b24d9fc705e433797ac64d"

# Use the same static-files crate and API as Meilisearch's build.rs.
GENERATOR = """
fn main() {
    let out = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
    static_files::resource_dir(out.join("mini-dashboard")).build().unwrap();
}
"""


def prepare(args):
    source = args.archive.read_bytes()
    if (
        hashlib.sha1(source).hexdigest() != SHA1
        or hashlib.sha256(source).hexdigest() != SHA256
    ):
        raise ValueError("unexpected mini-dashboard v0.4.1 archive")
    out = args.out_dir.resolve(strict=True)
    checksum = out / ".mini-dashboard.sha1"
    target = out / "mini-dashboard"
    if checksum.exists() and checksum.read_text() != SHA1:
        raise ValueError("the existing cache is for a different dashboard")
    lib = args.static_files_rlib.resolve(strict=True)
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        src = Path(tmp) / "dashboard.rs"
        prog = Path(tmp) / "generate-dashboard"
        src.write_text(GENERATOR)
        subprocess.run(
            [
                args.rustc,
                "--edition=2021",
                src,
                "-L",
                "dependency=" + str(lib.parent),
                "--extern",
                "static_files=" + str(lib),
                "-o",
                prog,
            ],
            check=True,
        )
        with zipfile.ZipFile(io.BytesIO(source)) as archive:
            if target.exists():
                files = {
                    p.relative_to(target).as_posix()
                    for p in target.rglob("*")
                    if p.is_file()
                }
                expected = {
                    item.filename for item in archive.infolist() if not item.is_dir()
                }
                if files != expected or any(
                    (target / p).read_bytes() != archive.read(p) for p in expected
                ):
                    raise ValueError(
                        "existing dashboard files differ from the release archive"
                    )
            else:
                stage = Path(tmp) / "mini-dashboard"
                archive.extractall(stage)
                stage.rename(target)
        subprocess.run([prog], env=dict(os.environ, OUT_DIR=str(out)), check=True)
        if not (out / "generated.rs").is_file():
            raise ValueError("static resource generation failed")
        checksum.write_text(SHA1)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for param in ("archive", "out-dir", "static-files-rlib", "rustc"):
        p.add_argument("--" + param, required=True, type=Path)
    prepare(p.parse_args())
