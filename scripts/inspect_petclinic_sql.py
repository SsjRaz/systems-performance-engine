"""Separate real-request SQL diagnostic for the preserved owner-lookup edit."""
import argparse
import datetime
import json
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import compare_petclinic as runner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--petclinic', type=Path, default=runner.ROOT.parent/'spring-petclinic')
    parser.add_argument('--port', type=int, default=8081)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, runner.interrupted)
    args.engine = runner.ROOT/'build/release/spe'
    args.k6 = args.engine  # Unused: only readiness and individual HTTP requests are sent.
    args.tool, args.scenario, args.same_artifact = 'spe', 'existing', None
    args.smoke, args.diagnostics = True, False
    args.warmup_requests = args.requests = args.conditioning_requests = args.runs = 1
    if not 1 <= args.port <= 65535:
        parser.error('Invalid port')
    with runner.exclusive_run(runner.ROOT/'build/petclinic-results/.comparison.lock'):
        engine, petclinic, source, metadata, jars, hashes, java, _ = runner.prepare(args)
        if runner.pids(['lsof', '-nP', '-t', f'-iTCP:{args.port}', '-sTCP:LISTEN']):
            raise RuntimeError('Diagnostic port is occupied; nothing was stopped')
        output = Path(tempfile.mkdtemp(prefix='sql-check-', dir=runner.ROOT/'build/validation'))
        evidence = dict(status='running', scope='SQL execution evidence only; logging changes runtime cost',
            started_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            jar_sha256=hashes, source_metadata=metadata, source_sha256=runner.digest(Path(__file__)),
            variants={})
        runner.save(output/'evidence.json', evidence)
        shutil.copy2(source/'candidate.patch', output/'candidate.patch')
        expected = (source/'baseline-owner.html').read_bytes()
        environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'}
        base = f'http://127.0.0.1:{args.port}'
        for variant in ('baseline', 'candidate'):
            runner.engine_idle(engine)
            if runner.digest(jars[variant]) != hashes[variant]:
                raise RuntimeError('Application artifact changed')
            block = output/variant; block.mkdir()
            command = [str(java), '-Xms512m', '-Xmx512m', '-jar', str(jars[variant]),
                '--server.address=127.0.0.1', f'--server.port={args.port}',
                '--spring.config.location=classpath:/application.properties', '--spring.profiles.active=default',
                '--database=h2', '--logging.level.root=INFO', '--logging.level.org.hibernate.SQL=DEBUG',
                '--spring.jpa.properties.hibernate.generate_statistics=true', '--debug=false', '--trace=false']
            runner.save(block/'server-command.json', command)
            observations=[]
            with (block/'server.log').open('x') as log:
                process = subprocess.Popen(command,cwd=petclinic,env=environment,stdout=log,
                    stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True)
                try:
                    runner.ready(process,base,expected)
                    if runner.pids(['lsof','-nP','-t',f'-iTCP:{args.port}','-sTCP:LISTEN']) != {str(process.pid)}:
                        raise RuntimeError('Diagnostic listener is not the launched JVM')
                    for number in range(1,6):
                        offset=(block/'server.log').stat().st_size
                        body=runner.get(base+'/owners/1')
                        if body != expected: raise RuntimeError('Owner response changed')
                        time.sleep(0.05)  # Allow session-close diagnostics to flush after response completion.
                        excerpt=(block/'server.log').read_bytes()[offset:].decode(errors='replace')
                        (block/f'request-{number:02d}.sql.txt').write_text(excerpt)
                        (block/f'request-{number:02d}.html').write_bytes(body)
                        selects=[line for line in excerpt.splitlines()
                                 if 'org.hibernate.SQL' in line and 'select ' in line.lower()]
                        session_counts=re.findall(r'executing (\d+) JDBC statements',excerpt)
                        statements=sum(int(n) for n in session_counts) if session_counts else None
                        if not selects:
                            raise RuntimeError('No SQL captured; inspect logs')
                        observations.append(dict(request=number,status=200,body_sha256=runner.digest(block/f'request-{number:02d}.html'),
                            select_log_lines=len(selects),jdbc_statements_in_session_metrics=statements,
                            session_metrics_available=bool(session_counts)))
                        evidence['variants'][variant]=observations
                        runner.save(output/'evidence.json',evidence)
                finally:
                    runner.stop_child(process)
            print(variant,observations,flush=True)
        evidence['finished_at_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
        evidence['status']='complete'
        runner.save(output/'evidence.json',evidence)
        print(output,flush=True)


if __name__ == '__main__':
    main()
