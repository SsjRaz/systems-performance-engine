"""Bounded validation matrix reusing the Petclinic runner; no automatic verdicts."""
import argparse
import datetime
import json
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite',choices=('smoke','timing','controls','diagnostics'),default='smoke')
    parser.add_argument('--output-root',type=Path,default=ROOT/'build/validation')
    parser.add_argument('--petclinic',type=Path,default=ROOT.parent/'spring-petclinic')
    parser.add_argument('--engine',type=Path,default=ROOT/'build/release/spe')
    parser.add_argument('--k6',type=Path,default=ROOT/'build/tools/k6-v2.4.0/k6-v2.4.0-macos-arm64/k6')
    parser.add_argument('--port',type=int,default=8081)
    args=parser.parse_args(); args.output_root.mkdir(parents=True,exist_ok=True)
    output=Path(tempfile.mkdtemp(prefix=f'{args.suite}-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ-'),dir=args.output_root)).resolve()
    experiments=[]
    if args.suite=='smoke':
        experiments=[('same','spe',['--same-artifact','baseline']),('delay','k6',['--scenario','delay'])]
    elif args.suite=='controls':
        experiments=[('control','spe',['--scenario','control']),('control','k6',['--scenario','control'])]
    elif args.suite=='diagnostics':
        experiments=[('same','spe',['--same-artifact','baseline','--diagnostics']),
                     ('same','k6',['--same-artifact','baseline','--diagnostics']),
                     ('cpu-large','spe',['--scenario','cpu-large','--diagnostics'])]
    else:
        experiments=[('same-1','spe',['--same-artifact','baseline']),('same-1','k6',['--same-artifact','baseline']),
                     ('same-2','k6',['--same-artifact','baseline']),('same-2','spe',['--same-artifact','baseline']),
                     ('existing','spe',[]),('existing','k6',[]),('delay','spe',['--scenario','delay']),('delay','k6',['--scenario','delay']),
                     ('cpu-small','k6',['--scenario','cpu-small']),('cpu-small','spe',['--scenario','cpu-small']),
                     ('cpu-large','spe',['--scenario','cpu-large']),('cpu-large','k6',['--scenario','cpu-large'])]
    state=dict(suite=args.suite,status='running',experiments=[])
    (output/'suite.json').write_text(json.dumps(state,indent=2)+'\n')
    print('Validation suite:',output,flush=True)
    try:
        for i,(name,tool,extra) in enumerate(experiments,1):
            mode='--smoke' if args.suite=='smoke' else '--run'
            budgets=['--requests','20000','--warmup-requests','50000','--runs','2']
            if name in ('control','delay','cpu-large','cpu-small'): budgets=['--requests','10000','--warmup-requests','50000','--runs','2']
            if args.suite=='diagnostics': budgets=['--requests','20000','--warmup-requests','50000','--runs','2']
            command=[sys.executable,str(ROOT/'scripts/compare_petclinic.py'),mode,'--port',str(args.port),'--tool',tool,
                     '--petclinic',str(args.petclinic.resolve()),'--engine',str(args.engine.resolve()),'--k6',str(args.k6.resolve()),
                     '--output-root',str(output),*budgets,*extra]
            log=output/f'{i:02d}-{name}-{tool}.log'
            print(f'[{i}/{len(experiments)}] {name} / {tool}',flush=True)
            with log.open('x') as stream:
                process=subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
                try: code=process.wait(timeout=1200)
                finally:
                    if process.poll() is None:
                        process.send_signal(signal.SIGINT)
                        try: process.wait(timeout=30)
                        except subprocess.TimeoutExpired: process.kill(); process.wait()
            text=log.read_text()
            directories=[line[len('Results: '):] for line in text.splitlines() if line.startswith('Results: ')]
            state['experiments'].append(dict(name=name,tool=tool,command=command,log=str(log),returncode=code,
                                             directory=directories[-1] if directories else None))
            (output/'suite.json').write_text(json.dumps(state,indent=2)+'\n')
            if code: raise RuntimeError(f'Experiment failed; inspect {log}')
        state['status']='complete'
    except BaseException as error:
        state.update(status='failed',error=str(error)); raise
    finally: (output/'suite.json').write_text(json.dumps(state,indent=2)+'\n')
    print('Complete:',output,flush=True)


if __name__=='__main__': main()
