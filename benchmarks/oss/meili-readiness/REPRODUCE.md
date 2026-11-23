# Reproducing the Meilisearch upstream-readiness study

Use Linux x86-64, Python 3.11+, three assignable CPUs, a Rust 1.91.1 toolchain, GNU binutils and gperftools. Build SPE and prepare the two 100,000-document indexes using the existing OSS guide. The secondary indexing harness creates its own disposable databases. Keep builds and other benchmarks outside measurement windows. [Fixture and SPE setup](../README.md) · [Original paired protocol and arm-file format](../MEILI-PROFILE.md)

## Meilisearch binaries

Use clean checkouts of Meilisearch v1.41.0 (`8dd5a95`) and v1.42.0 (`02ff9a4`). Produce three executables per configuration. First build v1.41.0 and stock v1.42.0. Apply the archived patch to v1.42.0, rebuild and save the patched executable separately. Use the same options and toolchain within each comparison. Give independent checkouts separate Cargo target directories. When reusing one checkout for the v1.42.0 intervention, apply the patch after saving the stock binary and verify Cargo recompiles the changed crate. [Patch](../meili-share-filter-context.patch)

```bash
export RUSTFLAGS=-Cforce-frame-pointers=yes
export CARGO_PROFILE_RELEASE_DEBUG=1
export CARGO_PROFILE_RELEASE_STRIP=none
cargo build --locked --release -p meilisearch --bin meilisearch
```

For the reduced configuration add `--no-default-features` to that command. For a second-host comparison of the original study, transfer the original ELFs and confirm their SHA-256 hashes. A fresh source build can differ in ELF identity. The normal configuration includes all tokenizers and the mini-dashboard.

### Dictionary cache for the normal build

If the pinned Lindera asset host is unavailable, obtain the same dictionary versions from the Lindera organization's GitHub repositories. The v0.43.3 release CLI was built with the compression feature that Charabia enables. The helper verifies each dictionary input against its tagged Git blob or Git LFS SHA-256 and size before running the release builder. The resulting seven dictionary components per language are available through the crate's supported cache. Keep that cache fixed for all three builds. The dashboard archive must match the hard-coded upstream SHA-1 and the release SHA-256. [Lindera release workflow](https://github.com/lindera/lindera/blob/v0.43.3/.github/workflows/release.yml) · [Lindera cache handling](https://github.com/lindera/lindera/blob/v0.43.3/lindera-dictionary/src/assets.rs)

```bash
assets="$PWD/build/readiness-assets"
mkdir -p "$assets"
GIT_LFS_SKIP_SMUDGE=1 git clone -q --depth 1 --branch 2.1.2 \
    https://github.com/lindera/unidic-mecab.git "$assets/unidic-git"
GIT_LFS_SKIP_SMUDGE=1 git clone -q --depth 1 --branch 2.1.1-20180720 \
    https://github.com/lindera/mecab-ko-dic.git "$assets/korean-git"
curl -fL https://api.github.com/repos/lindera/unidic-mecab/tarball/2.1.2 \
    -o "$assets/unidic-source.tgz"
curl -fL https://api.github.com/repos/lindera/mecab-ko-dic/tarball/2.1.1-20180720 \
    -o "$assets/korean-source.tgz"
curl -fL https://github.com/lindera/lindera/releases/download/v0.43.3/lindera-x86_64-unknown-linux-gnu-v0.43.3.zip \
    -o "$assets/lindera-cli.zip"
export LINDERA_CACHE="$PWD/build/readiness-dictionaries"
python3 benchmarks/oss/meili-readiness/dictionary_cache.py --assets "$assets" \
    --cache "$LINDERA_CACHE" --report "$assets/provenance.json"
```

If the build script reports `UnknownIssuer` for the dashboard, download the official v0.4.1 archive with the system TLS trust store. Give the helper the failed Meilisearch build's `OUT_DIR`, the release build's host `libstatic_files-*.rlib` and its `rustc`. It runs the same static resource generator as upstream and writes the cache marker after success. Rerun the original locked build. The HTTP checks verify the embedded HTML and JavaScript against the release bytes. [Dashboard release](https://github.com/meilisearch/mini-dashboard/releases/tag/v0.4.1) · [Upstream build script](https://github.com/meilisearch/meilisearch/blob/02ff9a4a1cae79ce405638b6323fbd2f939f7f67/crates/meilisearch/build.rs)

```bash
curl -fL https://github.com/meilisearch/mini-dashboard/releases/download/v0.4.1/build.zip -o "$assets/dashboard.zip"
python3 benchmarks/oss/meili-readiness/dashboard_cache.py --archive "$assets/dashboard.zip" \
    --out-dir "$dashboard_out" --static-files-rlib "$static_files" --rustc "$rustc"
```

### Parser allocation diagnostic

Copy `alloc.rs` into `crates/filter-parser/examples/spe_alloc.rs` in each private v1.42.0 checkout. Use a **new, separate target directory for each arm**. Run the tests and verify that the patched arm runs the named mixed-context test before using its counts. The standalone, single-threaded example wraps the system allocator and counts requests made only during `IndexFilterCondition::into_owned`. Parsing, formatting, deallocation and the HTTP server are outside this measurement. [Diagnostic source](alloc.rs)

```bash
cargo test --locked --release -p filter-parser --lib --target-dir "$arm_target"
cargo run --quiet --locked --release -p filter-parser --example spe_alloc --target-dir "$arm_target"
```

## Collect measurements

For each configuration create two arm files in the format from PR #10. Both specify the patched v1.42.0 executable as candidate. The intervention's baseline is stock v1.42.0 and the residual baseline is v1.41.0. Paths and SHA-256 hashes must identify the binaries on this machine. Set `study` to a fresh output directory. The same measured SPE must be used for timing and for collecting profiles. `analysis` points to an SPE build that supports v2 profile manifests. The recorded seeds for both configurations are in the driver. [Protocol](DESIGN.md)

```bash
driver=benchmarks/oss/meili-readiness/campaign.py
for config in portable default; do
    opts=(--configuration "$config" --host-label intel --output "$study"
          --engine "$spe" --analysis-engine "$analysis" --fixture "$fixture" --tools "$tools"
          --intervention "$arms/$config/intervention.json" --residual "$arms/$config/residual.json"
          --library "$libprofiler")
    python3 "$driver" primary "${opts[@]}"
    python3 "$driver" controls "${opts[@]}"
done
```

For each configuration this runs 32 primary timing pairs, four diagnostic sampling pairs if SPE confirms a residual regression, 24 indexing pairs and 24 secondary search pairs. Each secondary search server measures four workloads in a within-pair matched order. The default configuration also runs two short-list comparisons of eight pairs each. A separate four-pair diagnostic collects eight stage reports after one warm-up per server. Each 512-document indexing batch must complete successfully and each full retrieved corpus must match the generator. Successful response digests cover the ordered payload and metadata after removing request UUID and processing time. Error comparisons include the full message, status, type, code and link.

## Replay

Extract the evidence bundle linked in the results. From its `meili-readiness` directory run `sha256sum --quiet -c SHA256SUMS`. From the SPE repository run the command below with the extracted directory as its first argument. It replays the native decisions and checks captured samples, native successes, application receipts, semantic comparisons and stage summaries. Add repeated `--elf /path/to/measured/executable` to resolve ELF-relative stacks again. Exact measured ELF hashes are required for re-symbolization.

```bash
python3 benchmarks/oss/meili-readiness/replay.py "$extracted/meili-readiness" \
    --engine "$analysis"
```
