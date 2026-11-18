# Local profiling evidence

The 42 recordings from the November 2026 validation runs are kept as lossless gzip files under `local-evidence/profiling-20261115/`, outside `build/`. This directory is excluded from Git. Keep it when clearing build outputs; Git alone does not back up the recordings.

The tracked `manifest.json` records original paths, sizes, modification times, original SHA-256 hashes, and compressed SHA-256 hashes. The tracked restoration utility checks both compressed and decompressed contents. It never overwrites a conflicting original.

From the repository root:

```sh
python3 scripts/restore_profiles.py
python3 scripts/restore_profiles.py --restore
```

The first command verifies without restoring. The second recreates the exact original paths expected by existing analysis scripts, creating missing build subdirectories when necessary. Compressed copies remain intact. Restoring everything requires approximately 3.375 GB of additional space.

To restore one recording, supply its `original` path from the manifest:

```sh
python3 scripts/restore_profiles.py --restore --file 'PATH_FROM_MANIFEST'
```

Ordinary benchmark result JSON and diagnostic summaries were not compressed. Restoring raw recordings does not rerun an experiment. Keep the local archives together with this manifest and utility when moving or backing up the project. Historical paths inside the original audit records describe their location before archival relocation; this manifest is the current archive-location index.
