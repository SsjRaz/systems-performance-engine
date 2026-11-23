"""Collect the declared, non-overlapping process pairs for one Meilisearch feature configuration."""

import argparse
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meili_profile import read, store

HERE = Path(__file__).resolve().parent


def run(*args):
    subprocess.run(list(map(str, args)), check=True)


def campaign(args):
    dest = args.output.resolve()
    dest.mkdir(exist_ok=True, parents=True)
    config = args.configuration
    iv, residual = read(args.intervention), read(args.residual)
    if iv["candidate"] != residual["candidate"]:
        raise ValueError("comparisons need the same patched build")
    aa = dest / (config + "-aa-arms.json")
    same = {k: iv["baseline"] for k in ("baseline", "candidate")}
    if aa.exists():
        if read(aa) != same:
            raise ValueError("different unchanged-code build")
    else:
        store(aa, same)
    specs = [("aa", aa), ("patch", args.intervention), ("residual", args.residual)]
    base = 1 if config == "portable" else 2
    common = ["--engine", args.engine, "--fixture", args.fixture]
    if args.phase == "primary":
        for i, (name, arms) in enumerate(specs, 1):
            directory = dest / f"{config}-{name}"
            flags = [
                *common,
                "--tools",
                args.tools,
                "--arms",
                arms,
                "--output",
                directory,
                "--name",
                f"{args.host_label}-{config}-{name}",
                "--pairs",
                8 if name == "aa" else 12,
                "--seed",
                84000 + base * 100 + i,
            ]
            run(sys.executable, HERE.parent / "meili_profile.py", "timing", *flags)
            if (
                name == "residual"
                and read(directory / "decision.json")["verdict"] == "regression"
            ):
                run(
                    sys.executable,
                    HERE.parent / "meili_profile.py",
                    "profile",
                    *flags,
                    "--library",
                    args.library,
                )
                run(
                    args.analysis_engine,
                    "profile-diff",
                    directory / "profile.json",
                    "--json",
                    directory / "ranking.json",
                )
    else:
        for j, phase in enumerate(("index", "catalog", "stages", "short-lists")):
            for i, (name, arms) in enumerate(specs):
                if phase == "short-lists" and (
                    config != "default" or name == "residual"
                ):
                    continue
                if phase == "stages" and name != "residual":
                    continue
                key = f"{config}-{phase}-{name}"
                flags = ["--default-features"] if config == "default" else []
                run(
                    sys.executable,
                    HERE / "validation.py",
                    phase,
                    *common,
                    "--arms",
                    arms,
                    "--output",
                    dest / key,
                    "--name",
                    key,
                    "--seed",
                    85000 + base * 100 + j * 10 + i,
                    *flags,
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("primary", "controls"))
    parser.add_argument(
        "--configuration", choices=("portable", "default"), required=True
    )
    parser.add_argument("--host-label", default="replica")
    for key in (
        "fixture",
        "tools",
        "output",
        "engine",
        "analysis-engine",
        "intervention",
        "residual",
        "library",
    ):
        parser.add_argument("--" + key, required=True, type=Path)
    campaign(parser.parse_args())
