"""Actual-HTTP accounting cross-check; optional k6 dependency, no public network."""
import argparse
import json
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import compare_petclinic as runner
import reference_k6
from integration import RunningService


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root',type=Path,default=Path('build/validation'))
    args=parser.parse_args(); args.output_root.mkdir(parents=True,exist_ok=True)
    output=Path(tempfile.mkdtemp(prefix='reference-accounting-',dir=args.output_root)).resolve()
    engine=Path('build/release/spe').resolve()
    k6=Path('build/tools/k6-v2.4.0/k6-v2.4.0-macos-arm64/k6').resolve()
    cases=[('success','/ok','successes'),('http_503','/ok?status=503','http_errors'),
           ('redirect','/ok?status=302','http_errors'),('delay','/ok?delay_ms=20','successes'),
           ('timeout','/ok?delay_ms=250','timeouts'),('truncated','/truncated','network_errors'),
           ('disconnect','/disconnect','network_errors'),('refused',None,'connection_errors')]
    evidence=[]
    for name,path,outcome in cases:
        for tool in ('spe','k6'):
            with RunningService() as service:
                if path is None:
                    with socket.socket() as reserved:
                        reserved.bind(('127.0.0.1',0)); url=f'http://127.0.0.1:{reserved.getsockname()[1]}/'
                else: url=service.url+path
                timeout=50 if name=='timeout' else 1000
                prefix=output/f'{name}-{tool}'
                if tool=='spe':
                    command=[str(engine),url,'--requests','80','--concurrency','4','--timeout-ms',str(timeout),'--json',str(prefix.with_suffix('.json'))]
                    p=subprocess.run(command,capture_output=True,text=True,timeout=30)
                    prefix.with_suffix('.stdout').write_text(p.stdout); prefix.with_suffix('.stderr').write_text(p.stderr)
                    assert p.returncode == (0 if outcome=='successes' else 3),(name,p.stderr)
                    result=json.loads(prefix.with_suffix('.json').read_text())
                else:
                    result=reference_k6.run(k6,url,80,4,timeout,prefix,{'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C'})
                assert result['counts'][outcome]==result['counts']['completed']==80,(name,tool,result['counts'])
                assert result['counts']['cancelled']==result['counts']['not_started']==0
                state=service.idle()
                if name in ('success','delay','http_503','redirect'):
                    assert state['received']==80 and state['peak']<=4,(name,tool,state)
                if name=='delay': assert result['success_latency_ms']['p50']>=15
                evidence.append(dict(case=name,tool=tool,expected=outcome,counts=result['counts'],server_observed=state))
                print(name,tool,'PASS',flush=True)
                (output/'checks.json').write_text(json.dumps(evidence,indent=2)+'\n')
    print(output)


if __name__=='__main__': main()
