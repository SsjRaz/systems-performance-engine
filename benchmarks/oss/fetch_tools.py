"""Fetch hash-pinned Linux amd64 release binaries into a user-selected directory."""

import argparse
import lzma
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.request

from benchlib import digest
from gitea_bench import BINARY_SHA, VERSION
from meilisearch_bench import K6_SHA, RELEASES

K6_ARCHIVE = "5eb2236bc76a3c5cced12aaf0fd76c4b1ceac5ea040c70388a3d272b46595b59"
GITEA_ARCHIVE = "0ddb403f49d55bff685a1eece2d94a25c31524de9e1f119d387382f31ee2bdbb"


def install(out, name, url, download_hash, binary_hash):
    target = out / name
    if target.exists():
        if digest(target) != binary_hash:
            raise RuntimeError(f"Unexpected existing file: {target}")
        return
    with tempfile.TemporaryDirectory(dir=out) as scratch:
        source = Path(scratch) / "asset"
        with urllib.request.urlopen(url, timeout=90) as stream, source.open("xb") as f:
            while chunk := stream.read(1024 * 1024):
                f.write(chunk)
                if f.tell() > 160 * 1024 * 1024:
                    raise RuntimeError("Release archive exceeds size budget")
        if digest(source) != download_hash:
            raise RuntimeError(f"Release checksum mismatch: {name}")
        binary = Path(scratch) / "binary"
        if url.endswith(".xz"):
            with lzma.open(source) as compressed, binary.open("xb") as f:
                shutil.copyfileobj(compressed, f)
        elif url.endswith(".tar.gz"):
            with tarfile.open(source, "r:gz") as archive, binary.open("xb") as f:
                shutil.copyfileobj(archive.extractfile("k6-v1.0.0-linux-amd64/k6"), f)
        else:
            binary = source
        if digest(binary) != binary_hash:
            raise RuntimeError(f"Executable checksum mismatch: {name}")
        binary.chmod(0o700)
        binary.rename(target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    dest = parser.parse_args().output
    dest.mkdir(parents=True, exist_ok=True)
    for version, sha in RELEASES.values():
        install(
            dest,
            "meili-" + version,
            f"https://github.com/meilisearch/meilisearch/releases/download/v{version}/meilisearch-linux-amd64",
            sha,
            sha,
        )
    install(
        dest,
        "k6",
        "https://github.com/grafana/k6/releases/download/v1.0.0/k6-v1.0.0-linux-amd64.tar.gz",
        K6_ARCHIVE,
        K6_SHA,
    )
    install(
        dest,
        "gitea-" + VERSION,
        f"https://github.com/go-gitea/gitea/releases/download/v{VERSION}/gitea-{VERSION}-linux-amd64.xz",
        GITEA_ARCHIVE,
        BINARY_SHA,
    )
