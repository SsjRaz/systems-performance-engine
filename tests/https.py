"""Actual local TLS connections and certificate-policy checks; not a capacity benchmark."""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import signal
import socketserver
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from http_server import Server
from integration import HttpIntegration, RunningService


def certificates(directory, openssl):
    """Generate a private test PKI without modifying any operating-system trust store."""
    directory.mkdir()
    commands = []
    def run(*args, expected=0):
        command = [str(openssl), *map(str,args)]
        result = subprocess.run(command, text=True, capture_output=True, timeout=30)
        commands.append(dict(command=command,returncode=result.returncode,stdout=result.stdout,stderr=result.stderr))
        (directory/'commands.json').write_text(json.dumps(commands,indent=2)+'\n')
        if result.returncode != expected:
            raise RuntimeError(f'Certificate fixture command failed: {command}; {result.stderr}')
        return result
    def quoted(path):
        return '"'+str(path).replace('\\','\\\\').replace('$','\\$').replace('"','\\"')+'"'
    for name in ('root','other-root'):
        config=directory/(name+'.cnf')
        config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ca\nprompt=no\n'
            f'[dn]\nCN=SPE Local Test {name}\n[ca]\nbasicConstraints=critical,CA:TRUE\n'
            'keyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n')
        run('req','-new','-x509','-newkey','rsa:2048','-nodes','-sha256','-days','2',
            '-config',config,'-keyout',directory/(name+'.key'),'-out',directory/(name+'.pem'))
    run('req','-new','-newkey','rsa:2048','-nodes','-sha256','-subj','/CN=localhost',
        '-keyout',directory/'server.key','-out',directory/'server.csr')
    (directory/'index.txt').write_text('')
    (directory/'serial').write_text('1000\n')
    signer=directory/'signer.cnf'
    signer.write_text('[ca]\ndefault_ca=signer\n[signer]\n'+
        ''.join(f'{key}={quoted(directory/value)}\n' for key,value in
                [('database','index.txt'),('serial','serial'),('new_certs_dir','.'),
                 ('certificate','root.pem'),('private_key','root.key')])+
        'default_md=sha256\ndefault_days=1\npolicy=policy\nunique_subject=no\n[policy]\ncommonName=supplied\n')
    for name in ('valid','expired','mismatch'):
        extension=directory/(name+'.ext')
        names='DNS:wrong-host.invalid' if name=='mismatch' else 'DNS:localhost,IP:127.0.0.1'
        extension.write_text('[server]\nbasicConstraints=critical,CA:FALSE\n'
            'keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n'
            f'subjectAltName={names}\n')
        dates=['-startdate','20100101000000Z','-enddate','20200101000000Z'] if name=='expired' else ['-days','1']
        run('ca','-batch','-notext','-config',signer,'-extensions','server','-extfile',extension,
            '-in',directory/'server.csr','-out',directory/(name+'.pem'),*dates)
        run('x509','-in',directory/(name+'.pem'),'-noout','-dates','-subject','-issuer')
    run('verify','-CAfile',directory/'root.pem','-purpose','sslserver','-verify_hostname','localhost',directory/'valid.pem')
    expired=run('verify','-CAfile',directory/'root.pem','-purpose','sslserver',directory/'expired.pem',expected=2)
    if 'expired' not in (expired.stdout+expired.stderr).lower():
        raise RuntimeError('Expired fixture did not fail for certificate expiration')
    run('verify','-CAfile',directory/'root.pem','-purpose','sslserver','-verify_hostname','localhost',directory/'mismatch.pem',expected=2)
    for path in directory.glob('*.key'):
        path.chmod(0o600)


class SecureServer(Server):
    def __init__(self, context):
        self.context = context
        self.tls_lock = threading.Lock()
        self.tls = []
        self.handshake_failures = 0
        super().__init__(('127.0.0.1',0))

    def get_request(self):
        raw, address = super().get_request()
        try:
            secure = self.context.wrap_socket(raw,server_side=True,do_handshake_on_connect=False)
            secure.settimeout(3)
            return secure,address
        except BaseException:
            raw.close()
            raise

    def process_request_thread(self, request, address):
        try:
            request.do_handshake()
            with self.tls_lock:
                self.tls.append(dict(version=request.version(),cipher=request.cipher()[0],session_reused=request.session_reused))
        except (ssl.SSLError,OSError):
            with self.tls_lock:
                self.handshake_failures += 1
            self.shutdown_request(request)
            return
        super().process_request_thread(request,address)


class RunningTLS(RunningService):
    def __init__(self, certs, leaf='valid', client_certificate=False):
        self.certs,self.leaf,self.client_certificate=certs,leaf,client_certificate

    def __enter__(self):
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version=ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.certs/(self.leaf+'.pem'),self.certs/'server.key')
        if self.client_certificate:
            context.maximum_version=ssl.TLSVersion.TLSv1_2
            context.verify_mode=ssl.CERT_REQUIRED
            context.load_verify_locations(self.certs/'root.pem')
        self.server=SecureServer(context)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':0.02})
        self.thread.start()
        self.url=f'https://127.0.0.1:{self.server.server_port}'
        return self

    def observed(self):
        state=self.idle()
        with self.server.tls_lock:
            state.update(tls_handshakes=list(self.server.tls),failed_handshakes=self.server.handshake_failures)
        return state


class StallHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        try:
            header=self.request.recv(5)
            with self.server.lock:
                self.server.client_hellos.append(header.hex())
            self.server.done.wait(5)
        except OSError:
            pass


class StalledHandshake:
    def __enter__(self):
        class Listener(socketserver.ThreadingTCPServer):
            daemon_threads=True
            block_on_close=False
        self.server=Listener(('127.0.0.1',0),StallHandler)
        self.server.lock=threading.Lock()
        self.server.done=threading.Event()
        self.server.client_hellos=[]
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':0.02})
        self.thread.start()
        self.url=f'https://127.0.0.1:{self.server.server_address[1]}'
        return self

    def __exit__(self,*_):
        self.server.done.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class HttpsIntegration(unittest.TestCase):
    engine=artifacts=certs=None
    observations=[]

    def command(self,url,requests=12,concurrency=2,timeout=2000,ca=True,extra=()):
        args=[str(self.engine),url,'--requests',str(requests),'--concurrency',str(concurrency),
              '--timeout-ms',str(timeout),'--json','-']
        if ca:
            args+=['--ca-file',str(self.certs/'root.pem' if ca is True else ca)]
        return [*args,*map(str,extra)]

    def retain(self,name,args,process,stdout,stderr,elapsed,expected):
        prefix=self.artifacts/name
        prefix.with_suffix('.stdout').write_text(stdout)
        prefix.with_suffix('.stderr').write_text(stderr)
        prefix.with_suffix('.command.json').write_text(json.dumps(args,indent=2)+'\n')
        self.assertEqual(process.returncode,expected,(args,stdout,stderr))
        result=json.loads(stdout)
        prefix.with_suffix('.json').write_text(json.dumps(result,indent=2)+'\n')
        self.assertEqual(result['schema_version'],3)
        HttpIntegration.check_accounting(self,result)
        tls=result['workload']['tls']
        self.assertEqual(tls['verify_peer'],True)
        self.assertEqual(tls['verify_host'],True)
        self.assertEqual(tls['minimum_version'],'1.2')
        self.assertNotIn(str(self.certs),stdout+stderr)
        self.assertNotIn('BEGIN CERTIFICATE',stdout+stderr)
        self.observations.append(dict(name=name,command=args,returncode=process.returncode,
            process_wall_seconds=elapsed,result=result))
        print(f"\n{name}: success={result['counts']['successes']} TLS={result['counts']['tls_errors']} "
              f"timeout={result['counts']['timeouts']} elapsed={result['elapsed_seconds']:.6f}s",flush=True)
        return result

    def execute(self,name,url,expected=0,**options):
        args=self.command(url,**options)
        environment=dict(os.environ,https_proxy='http://127.0.0.1:1',HTTPS_PROXY='http://127.0.0.1:1',ALL_PROXY='http://127.0.0.1:1',NO_PROXY='',no_proxy='')
        started=time.monotonic()
        result=subprocess.run(args,text=True,capture_output=True,timeout=20,env=environment)
        return self.retain(name,args,result,result.stdout,result.stderr,time.monotonic()-started,expected)

    def observed(self,name,state):
        next(r for r in reversed(self.observations) if r['name']==name)['server_observed']=state

    def compare(self,name,a,b,expected=0,extra=()):
        command=[str(self.engine),'compare',str(self.artifacts/(a+'.json')),str(self.artifacts/(b+'.json')),*extra,'--json','-']
        result=subprocess.run(command,text=True,capture_output=True,timeout=10)
        (self.artifacts/(name+'.comparison.stdout')).write_text(result.stdout)
        (self.artifacts/(name+'.comparison.stderr')).write_text(result.stderr)
        self.assertEqual(result.returncode,expected,(result.stdout,result.stderr))
        return json.loads(result.stdout)

    def test_trusted_certificate_concurrency_and_reuse(self):
        for concurrency in (1,3):
            with self.subTest(concurrency=concurrency),RunningTLS(self.certs) as service:
                name=f'trusted-c{concurrency}'
                result=self.execute(name,service.url+'/ok?delay_ms=20',requests=30,concurrency=concurrency)
                state=service.observed();self.observed(name,state)
                self.assertEqual(state['received'],30)
                self.assertEqual(state['finished'],30)
                self.assertLessEqual(state['peak'],concurrency)
                self.assertGreaterEqual(state['peak'],min(concurrency,2))
                self.assertEqual(state['connections'],concurrency)
                self.assertEqual(len(state['tls_handshakes']),concurrency)
                self.assertTrue(all(h['version'] in ('TLSv1.2','TLSv1.3') for h in state['tls_handshakes']))
                policy=result['workload']['tls']
                self.assertEqual(policy['ca_source'],'custom_bundle')
                self.assertEqual(policy['ca_bundle_sha256'],hashlib.sha256((self.certs/'root.pem').read_bytes()).hexdigest())
                self.assertEqual(result['counts']['tls_errors'],0)

    def test_certificate_validation_failures(self):
        cases=[('untrusted','valid',False),('wrong-ca','valid',self.certs/'other-root.pem'),
               ('expired','expired',True),('hostname-mismatch','mismatch',True)]
        for name,leaf,ca in cases:
            with self.subTest(case=name),RunningTLS(self.certs,leaf) as service:
                result=self.execute(name,service.url+'/ok',expected=3,requests=4,ca=ca)
                state=service.observed();self.observed(name,state)
                self.assertEqual(result['counts']['tls_errors'],4)
                self.assertEqual(result['counts']['network_errors'],0)
                self.assertEqual(result['counts']['successes'],0)
                self.assertEqual(result['success_latency_ms']['samples'],0)
                self.assertEqual(state['received'],0)
                self.assertIn('60',result['transport_error_counts'])
                self.assertEqual(result['workload']['tls']['ca_source'],'libcurl_default' if ca is False else 'custom_bundle')

    def test_actual_tls_handshake_rejection(self):
        with RunningTLS(self.certs,client_certificate=True) as service:
            result=self.execute('handshake-rejected',service.url+'/ok',expected=3,requests=4)
            state=service.observed();self.observed('handshake-rejected',state)
            self.assertEqual(result['counts']['tls_errors'],4)
            self.assertEqual(state['received'],0)
            self.assertGreater(state['failed_handshakes'],0)

    def test_http_outcomes_and_body_limit_over_tls(self):
        with RunningTLS(self.certs) as service:
            for name,path,outcome in [('https-http-error','/ok?status=503','http_errors'),
                                      ('https-redirect','/ok?status=302','http_errors'),
                                      ('https-truncated','/truncated','network_errors'),
                                      ('https-body-limit','/ok?bytes=1024','response_limit_errors')]:
                with self.subTest(case=name):
                    extra=['--max-body-bytes','64'] if outcome=='response_limit_errors' else []
                    result=self.execute(name,service.url+path,expected=3,requests=4,extra=extra)
                    self.assertEqual(result['counts'][outcome],4)
                    self.assertEqual(result['counts']['tls_errors'],0)
            self.observed('https-body-limit',service.observed())

    def test_https_never_follows_a_plain_http_redirect(self):
        with RunningService() as plain, RunningTLS(self.certs) as secure:
            secure.server.redirect_to=plain.url+'/ok'
            result=self.execute('https-downgrade-refused',secure.url+'/ok?status=302',expected=3,requests=4)
            self.assertEqual(result['counts']['http_errors'],4)
            self.assertEqual(result['counts']['tls_errors'],0)
            self.assertEqual(secure.observed()['received'],4)
            self.assertEqual(plain.idle()['received'],0)
            self.observed('https-downgrade-refused',dict(secure=secure.observed(),plain=plain.idle()))

    def test_timeouts_during_handshake_and_response_body(self):
        with StalledHandshake() as service:
            result=self.execute('handshake-timeout',service.url+'/ok',expected=3,requests=4,timeout=100)
            self.assertEqual(result['counts']['timeouts'],4)
            self.assertEqual(result['counts']['tls_errors'],0)
            self.assertTrue(service.server.client_hellos)
            self.assertTrue(all(header.startswith('16') for header in service.server.client_hellos))
            self.observed('handshake-timeout',dict(client_hellos=service.server.client_hellos))
        with RunningTLS(self.certs) as service:
            result=self.execute('https-body-timeout',service.url+'/slow-body?delay_ms=400',expected=3,requests=4,timeout=150)
            self.assertEqual(result['counts']['timeouts'],4)
            self.assertEqual(result['counts']['tls_errors'],0)
            state=service.observed()
            self.assertEqual(state['received'],4)  # Handshakes completed; responses stalled in the body phase.
            self.observed('https-body-timeout',state)

    def test_cancellation_during_handshake_and_secure_body(self):
        for phase in ('handshake','body'):
            for interrupt in (signal.SIGINT,signal.SIGTERM):
                with self.subTest(phase=phase,signal=interrupt):
                    service=StalledHandshake() if phase=='handshake' else RunningTLS(self.certs)
                    with service:
                        url=service.url+('/ok' if phase=='handshake' else '/slow-body?delay_ms=2000')
                        args=self.command(url,requests=100,timeout=5000)
                        started=time.monotonic()
                        process=subprocess.Popen(args,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                        try:
                            deadline=time.monotonic()+5
                            while time.monotonic()<deadline and process.poll() is None:
                                ready=len(service.server.client_hellos)>=2 if phase=='handshake' else service.server.state.snapshot()['received']>=2
                                if ready: break
                                time.sleep(0.01)
                            else: self.fail('TLS workload did not reach the expected cancellation phase')
                            signal_at=time.monotonic()
                            process.send_signal(interrupt)
                            out,err=process.communicate(timeout=5)
                            self.assertLess(time.monotonic()-signal_at,2)
                            name=f'cancel-{phase}-{interrupt.name}'
                            result=self.retain(name,args,process,out,err,time.monotonic()-started,128+interrupt)
                            self.assertTrue(result['interrupted'])
                            self.assertEqual(result['counts']['cancelled'],2)
                            self.assertEqual(result['counts']['not_started'],98)
                            self.assertEqual(result['counts']['tls_errors'],0)
                        finally:
                            if process.poll() is None: process.kill();process.communicate()

    def test_ca_snapshot_and_safe_configuration_failures(self):
        original=self.artifacts/'selected-ca.pem'
        original.write_bytes((self.certs/'root.pem').read_bytes())
        with RunningTLS(self.certs) as service:
            args=self.command(service.url+'/ok?delay_ms=20&close=1',requests=12,concurrency=1,ca=original)
            started=time.monotonic()
            process=subprocess.Popen(args,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+5
                while service.server.state.snapshot()['received']<1 and process.poll() is None and time.monotonic()<deadline:
                    time.sleep(0.01)
                self.assertGreater(service.server.state.snapshot()['received'],0)
                original.write_bytes((self.certs/'other-root.pem').read_bytes())
                out,err=process.communicate(timeout=10)
                result=self.retain('ca-snapshot',args,process,out,err,time.monotonic()-started,0)
                self.assertEqual(result['workload']['tls']['ca_bundle_sha256'],hashlib.sha256((self.certs/'root.pem').read_bytes()).hexdigest())
                self.assertNotIn(str(original),out+err)
                self.observed('ca-snapshot',service.observed())
            finally:
                if process.poll() is None: process.kill();process.communicate()
        bad=self.artifacts/'malformed-ca.pem';bad.write_text('not a certificate\n')
        with RunningTLS(self.certs) as service:
            result=self.execute('malformed-ca',service.url+'/ok',expected=3,requests=2,ca=bad)
            self.assertEqual(result['counts']['tls_errors'],2)
            self.assertEqual(service.observed()['received'],0)
            empty=self.artifacts/'empty-ca.pem';empty.write_text('')
            fifo=self.artifacts/'ca-pipe';os.mkfifo(fifo)
            oversized=self.artifacts/'large-ca.pem';oversized.write_bytes(b'x'*(1024*1024+1))
            for path,code in ((self.artifacts/'absent.pem',1),(empty,2),(self.artifacts,2),(fifo,2),(oversized,2)):
                args=self.command(service.url+'/ok',requests=2,ca=path)
                process=subprocess.run(args,text=True,capture_output=True,timeout=5)
                self.assertEqual(process.returncode,code,(path,process.stdout,process.stderr))
                self.assertEqual(process.stdout,'')
                self.assertNotIn(str(path),process.stderr)
            oversized.unlink()
            self.assertEqual(service.observed()['received'],0)

    def test_saved_https_comparisons_and_policy_compatibility(self):
        copy=self.artifacts/'same-ca-different-path.pem'
        shutil.copyfile(self.certs/'root.pem',copy)
        with RunningTLS(self.certs) as a,RunningTLS(self.certs) as b:
            self.execute('compare-a',a.url+'/ok?delay_ms=5')
            self.execute('compare-b',b.url+'/ok?delay_ms=5',ca=copy)
            self.compare('origin-rejected','compare-a','compare-b',expected=2)
            report=self.compare('https-compatible','compare-a','compare-b',extra=['--allow-origin-change'])
            self.assertEqual(report['status'],'descriptive_comparison')
            self.assertEqual(report['input_result_schema_version'],3)
            with RunningService() as plain:
                command=[str(self.engine),plain.url+'/ok?delay_ms=5','--requests','12','--concurrency','2','--timeout-ms','2000',
                         '--json',str(self.artifacts/'compare-http.json')]
                subprocess.run(command,text=True,capture_output=True,check=True,timeout=10)
                self.compare('http-https-rejected','compare-http','compare-a',expected=2,extra=['--allow-origin-change'])
        with RunningTLS(self.certs,'expired') as service:
            for name in ('compare-failed-a','compare-failed-b'):
                self.execute(name,service.url+'/ok',expected=3)
            report=self.compare('failed-no-deltas','compare-failed-a','compare-failed-b',expected=3)
            self.assertEqual(report['status'],'insufficient_evidence')
            self.assertTrue(all(m['change_percent'] is None for m in report['metrics'].values()))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine',type=Path,required=True)
    parser.add_argument('--openssl',type=Path,required=True)
    parser.add_argument('--artifacts',type=Path,required=True)
    args=parser.parse_args()
    args.artifacts.mkdir(parents=True,exist_ok=True)
    directory=Path(tempfile.mkdtemp(prefix='https-',dir=args.artifacts)).resolve()
    started=datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        certificates(directory/'certificates',args.openssl)
        HttpsIntegration.engine=args.engine.resolve()
        HttpsIntegration.artifacts=directory
        HttpsIntegration.certs=directory/'certificates'
        suite=unittest.defaultTestLoader.loadTestsFromTestCase(HttpsIntegration)
        result=unittest.TextTestRunner(verbosity=2).run(suite)
    finally:
        # Preserve public certificate evidence, never archive fixture signing keys.
        for key in (directory/'certificates').glob('*.key'):
            key.unlink()
    (directory/'observations.json').write_text(json.dumps(dict(started_at_utc=started,
        finished_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),successful=result.wasSuccessful(),
        tests_run=result.testsRun,engine_sha256=hashlib.sha256(args.engine.read_bytes()).hexdigest(),
        python_tls=ssl.OPENSSL_VERSION,openssl=subprocess.check_output([str(args.openssl),'version'],text=True).strip(),
        observations=HttpsIntegration.observations,
        scope='Actual controlled local TLS integration checks, not a representative capacity or TLS-overhead benchmark.'),indent=2)+'\n')
    print('HTTPS artifacts:',directory,flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':
    raise SystemExit(main())
