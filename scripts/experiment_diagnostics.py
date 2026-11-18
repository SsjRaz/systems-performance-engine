"""Separate, instrumented JVM/resource observations; never used as timing baselines."""
import datetime
import json
import os
import shutil
import subprocess
import threading
import time


class Diagnostics:
    def __init__(self, pid, directory, java):
        self.pid, self.directory, self.java = pid, directory, java
        self.phase = 'startup'
        self.stop = threading.Event()
        self.profiles = {}
        self.thread = threading.Thread(target=self.sample, daemon=True)
        self.thread.start()

    def sample(self):
        with (self.directory / 'resource-samples.jsonl').open('x') as stream:
            while not self.stop.is_set():
                active = [str(self.pid)]
                for tool in ('spe','k6'):
                    p = subprocess.run(['pgrep','-x',tool],capture_output=True,text=True)
                    active.extend(p.stdout.split())
                    if self.phase == 'measurement' and shutil.which('sample'):
                        for pid in p.stdout.split():
                            if pid not in self.profiles:
                                path = self.directory / f'{tool}-{pid}.sample.txt'
                                command = ['sample', pid, '2', '10', '-file', str(path)]
                                output = (self.directory / f'{tool}-{pid}.sample.log').open('x')
                                try:
                                    process = subprocess.Popen(command,stdout=output,stderr=subprocess.STDOUT)
                                    self.profiles[pid] = (command, process, output)
                                except OSError as error:
                                    output.write(str(error)); output.close()
                p = subprocess.run(['ps','-p',','.join(active),'-o','pid=,pcpu=,rss=,vsz=,time=,comm='],capture_output=True,text=True)
                stream.write(json.dumps(dict(at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    monotonic=time.monotonic(), phase=self.phase, load_average=os.getloadavg(), ps=p.stdout, ps_error=p.stderr))+'\n')
                stream.flush()
                self.stop.wait(1)

    def snapshot(self, label):
        self.phase = label
        commands = [[str(self.java.with_name('jcmd')), str(self.pid), item]
                    for item in ('VM.uptime','GC.heap_info','Compiler.codecache','Compiler.queue')]
        commands += [[str(self.java.with_name('jstat')),'-compiler',str(self.pid)],
                     ['pmset','-g','therm'], ['pmset','-g','batt'], ['vm_stat'], ['top','-l','1','-n','8','-o','cpu']]
        data=[]
        for command in commands:
            try:
                p=subprocess.run(command,capture_output=True,text=True,timeout=15)
                data.append(dict(command=command,returncode=p.returncode,stdout=p.stdout,stderr=p.stderr))
            except (OSError,subprocess.TimeoutExpired) as error:
                data.append(dict(command=command,error=str(error)))
        (self.directory / f'{label}-diagnostics.json').write_text(json.dumps(data,indent=2)+'\n')

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)
        profiles=[]
        for command,process,output in self.profiles.values():
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
            finally: output.close()
            profiles.append(dict(command=command,returncode=process.returncode,
                                 note='Two-second macOS stack sample at 10 ms intervals; diagnostic overhead applies.'))
        (self.directory/'native-profiles.json').write_text(json.dumps(profiles,indent=2)+'\n')
        recording = self.directory/'recording.jfr'
        if recording.exists():
            command = [str(self.java.with_name('jfr')), 'summary', str(recording)]
            try:
                result = subprocess.run(command,capture_output=True,text=True,timeout=20)
                (self.directory/'jfr-summary.txt').write_text(result.stdout)
                (self.directory/'jfr-summary-status.json').write_text(json.dumps(dict(command=command,
                    returncode=result.returncode,stderr=result.stderr),indent=2)+'\n')
            except (OSError,subprocess.TimeoutExpired) as error:
                (self.directory/'jfr-summary-status.json').write_text(json.dumps(dict(command=command,error=str(error)))+'\n')


def jvm_flags(directory):
    return ['-XX:+UnlockDiagnosticVMOptions','-XX:+LogCompilation',f'-XX:LogFile={directory / "jit.xml"}',
            f'-Xlog:gc*,safepoint:file={directory / "gc.log"}:time,uptime,level,tags',
            f'-XX:StartFlightRecording=filename={directory / "recording.jfr"},settings=profile,maxsize=32m,dumponexit=true']
