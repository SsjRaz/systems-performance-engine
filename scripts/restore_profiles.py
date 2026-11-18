"""Verify profiling archives, or restore their original analysis paths without overwriting files."""
import argparse,gzip,hashlib,json,os,stat
from pathlib import Path
root=Path(__file__).resolve().parents[1]
def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        while data:=f.read(1024*1024):h.update(data)
    return h.hexdigest()
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--manifest',type=Path,default=root/'benchmarks/evidence/profiling-20261115/manifest.json')
parser.add_argument('--restore',action='store_true',help='Restore missing originals at their recorded paths; retain compressed copies')
parser.add_argument('--file',help='Select one original repository-relative path from the manifest')
args=parser.parse_args()
manifest=json.loads(args.manifest.read_text())
def require(condition,message):
    if not condition:raise RuntimeError(message)
rows=[r for r in manifest['files'] if not args.file or r['original']==args.file]
if not rows:parser.error('no matching original path in manifest')
for row in rows:
    target=root/row['original'];archive=root/row['archive']
    require(target.is_relative_to(root/'build') and archive.is_relative_to(root/'local-evidence'), 'unexpected evidence location')
    require('..' not in Path(row['original']).parts and '..' not in Path(row['archive']).parts, 'invalid evidence path')
    require(archive.resolve()==archive and target.parent.resolve()==target.parent, 'evidence paths must not use symlinks')
    require(digest(archive)==row['compressed_sha256'], 'compressed checksum mismatch: '+str(archive))
    if args.restore and target.exists():
        require(not target.is_symlink() and digest(target)==row['sha256'], 'existing original differs: '+str(target))
        print('Already restored: '+row['original']);continue
    temporary=target.with_name(target.name+'.restoring')
    output=None;h=hashlib.sha256();size=0
    if args.restore:
        target.parent.mkdir(parents=True,exist_ok=True)
        output=temporary.open('xb')
    try:
        with gzip.open(archive,'rb') as stream:
            while chunk:=stream.read(1024*1024):
                h.update(chunk);size+=len(chunk)
                if output:output.write(chunk)
        require(h.hexdigest()==row['sha256'] and size==row['original_bytes'], 'restored checksum mismatch: '+str(archive))
        if output:
            output.flush();os.fsync(output.fileno());output.close();output=None
            temporary.chmod(row['mode']);os.utime(temporary,ns=(row['mtime_ns'],row['mtime_ns']))
            os.link(temporary,target)  # Exclusive publication: never replace another original.
    finally:
        if output:output.close()
        if args.restore and temporary.exists():temporary.unlink()
print(('Restored' if args.restore else 'Verified')+' '+str(len(rows))+' profiling files; compressed evidence retained.')
