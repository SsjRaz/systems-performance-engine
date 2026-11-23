"""Populate Lindera's documented v0.43.3 Linux cache from its pinned GitHub sources."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import zipfile

TAGS = {
    "ko-dic": ("12439fb32808b9244ded56d7dfed96e4b8d76869", "korean"),
    "unidic": ("ca9b9856efadba0bdfe3a3e8f3374a4e20c1a055", "unidic"),
}
CLI_ZIP = "a19703abd59fbebfa45f81acf88e15e6536ea9d414ff0a9ab7b3e084c3dc1779"
FILES = {
    "char_def.bin",
    "matrix.mtx",
    "dict.da",
    "dict.vals",
    "unk.bin",
    "dict.wordsidx",
    "dict.words",
}


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def prepare(args):
    cache = args.cache.resolve() / "0.43.3"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        raise ValueError("select a fresh dictionary cache")
    archive = args.assets / "lindera-cli.zip"
    if digest(archive) != CLI_ZIP:
        raise ValueError("unexpected released Linux v0.43.3 Lindera CLI")
    report = {}
    with tempfile.TemporaryDirectory(dir=cache.parent) as tmp:
        scratch = Path(tmp)
        ready = scratch / "complete"
        ready.mkdir()
        cli = scratch / "lindera"
        with zipfile.ZipFile(archive) as z:
            cli.write_bytes(z.read("lindera"))
        cli.chmod(0o700)
        for kind, (commit, name) in TAGS.items():
            tree = args.assets / (name + "-git")

            def git(*argv):
                return subprocess.check_output(["git", "-C", tree, *argv])

            if git("rev-parse", "HEAD").decode().strip() != commit:
                raise ValueError("unexpected dictionary input commit")
            tracked = git("ls-tree", commit).decode().splitlines()
            expected = {
                p: meta.split()[2]
                for meta, p in (line.split("\t", 1) for line in tracked)
                if p.endswith((".csv", ".def"))
            }
            src = scratch / kind
            src.mkdir()
            inputs = {}
            source_archive = args.assets / (name + "-source.tgz")
            with tarfile.open(source_archive, "r|gz") as bundle:
                for member in bundle:
                    parts = Path(member.name).parts
                    if len(parts) != 2 or parts[1] not in expected:
                        continue
                    p = parts[1]
                    if not member.isfile() or p in inputs:
                        raise ValueError(p)
                    sha256 = hashlib.sha256()
                    blob = hashlib.sha1(f"blob {member.size}\0".encode())
                    with (
                        bundle.extractfile(member) as source,
                        (src / p).open("xb") as dest,
                    ):
                        while chunk := source.read(1024 * 1024):
                            sha256.update(chunk)
                            blob.update(chunk)
                            dest.write(chunk)
                    if blob.hexdigest() != expected[p]:
                        pointer = git("cat-file", "blob", expected[p]).decode()
                        if pointer.splitlines() != [
                            "version https://git-lfs.github.com/spec/v1",
                            "oid sha256:" + sha256.hexdigest(),
                            f"size {member.size}",
                        ]:
                            raise ValueError("LFS identity mismatch: " + p)
                    inputs[p] = sha256.hexdigest()
            if inputs.keys() != expected.keys():
                raise ValueError("dictionary archive is incomplete")
            # The released CLI is built with compress, matching Charabia's
            # feature selection. It calls the same versioned DictionaryBuilder.
            output = scratch / ("lindera-" + kind)
            subprocess.run([cli, "build", "-k", kind, src, output], check=True)
            if not FILES.issubset(p.name for p in output.iterdir()):
                raise ValueError("dictionary build is incomplete")
            report[kind] = dict(
                source_commit=commit,
                archive_sha256=digest(source_archive),
                inputs=inputs,
                output={p.name: digest(p) for p in output.iterdir() if p.is_file()},
            )
            output.rename(ready / output.name)
        ready.rename(cache)
    args.report.write_text(
        json.dumps(dict(cli_zip_sha256=CLI_ZIP, dictionaries=report), indent=2) + "\n"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("assets", "cache", "report"):
        p.add_argument("--" + name, type=Path, required=True)
    prepare(p.parse_args())
