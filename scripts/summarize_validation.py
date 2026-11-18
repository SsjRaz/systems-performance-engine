"""Summarize actual JVM-block observations; no hypothesis tests or verdict thresholds."""
import argparse
import csv
import datetime
import hashlib
import json
import re
import statistics
from pathlib import Path


def spread(values):
    return dict(n=len(values), values=values, mean=statistics.mean(values), minimum=min(values),maximum=max(values),
                sample_cv_percent=100*statistics.stdev(values)/statistics.mean(values) if len(values)>1 else None)


def clock_seconds(value):
    parts=[float(p) for p in value.split(':')]
    return sum(part*60**i for i,part in enumerate(reversed(parts)))


def diagnostics(path):
    manifest=json.loads((path/'manifest.json').read_text())
    result=dict(directory=str(path),tool=manifest['protocol']['tool'],scenario=manifest['protocol']['scenario'],
                same_artifact=manifest.get('same_artifact_verified'),blocks=[])
    for block in sorted(path.glob('[0-9][0-9]-*')):
        observation=dict(block=block.name)
        for name in ('execution-samples-summary', 'jfr-summary-status', 'native-profiles'):
            artifact=block/(name+'.json')
            if artifact.exists(): observation[name.replace('-','_')]=json.loads(artifact.read_text())
        windows=[json.loads(p.read_text()) for p in sorted(block.glob('run-[0-9][0-9].process.json'))]
        log=block/'gc.log'
        if log.exists():
            pauses=[float(m.group(1)) for line in log.read_text().splitlines()
                    if ' Pause ' in line and (m:=re.search(r' (\d+(?:\.\d+)?)ms$',line))]
            observation['gc_pause_ms_all_phases']=dict(count=len(pauses),total=sum(pauses),maximum=max(pauses) if pauses else None)
            measured=[]
            for line in log.read_text().splitlines():
                if ' Pause ' not in line or not (match:=re.search(r'^\[([^]]+)\].* (\d+(?:\.\d+)?)ms$',line)):
                    continue
                timestamp=datetime.datetime.fromisoformat(match.group(1))
                if any(datetime.datetime.fromisoformat(w['started_at_utc']) <= timestamp <=
                       datetime.datetime.fromisoformat(w['finished_at_utc']) for w in windows):
                    measured.append(float(match.group(2)))
            observation['gc_pause_ms_in_measured_process_intervals']=dict(count=len(measured),total=sum(measured),
                maximum=max(measured) if measured else None,
                note='Outer client process intervals include startup/aggregation; profiler overhead remains.')
        compiler={}
        memory={}
        power={}
        for label in ('after-warmup','after-measurement'):
            p=block/f'{label}-diagnostics.json'
            if not p.exists(): continue
            for item in json.loads(p.read_text()):
                if '-compiler' in item['command'] and item.get('returncode')==0:
                    lines=item['stdout'].strip().splitlines()
                    if len(lines)>=2:
                        compiler[label]=dict(zip(lines[-2].split(),lines[-1].split()))
                if item['command'][0]=='vm_stat' and item.get('returncode')==0:
                    memory[label]={match.group(1):int(match.group(2)) for line in item['stdout'].splitlines()
                                   if (match:=re.match(r'^([^:]+):\s+(\d+)\.',line))}
                if item['command'][0]=='pmset':
                    power.setdefault(label,[]).append(item.get('stdout',item.get('error','')))
        observation['jstat_compiler']=compiler
        if all(label in compiler for label in ('after-warmup','after-measurement')):
            observation['compilations_during_measurement_interval']=int(compiler['after-measurement']['Compiled'])-int(compiler['after-warmup']['Compiled'])
            observation['compiler_seconds_during_measurement_interval']=float(compiler['after-measurement']['Time'])-float(compiler['after-warmup']['Time'])
            observation['compiler_interval_note']='Includes diagnostic attachment/snapshot time around measurement; not precisely the native active window.'
        observation['memory_page_snapshots']=memory
        observation['power_snapshots']=power
        if all(label in memory for label in ('after-warmup','after-measurement')):
            observation['memory_counter_deltas']={key:memory['after-measurement'][key]-memory['after-warmup'][key]
                for key in ('Pageins','Pageouts','Swapins','Swapouts','Compressions','Decompressions')
                if key in memory['after-warmup'] and key in memory['after-measurement']}
        processes={}
        loads=[]
        p=block/'resource-samples.jsonl'
        if p.exists():
            for line in p.read_text().splitlines():
                sample=json.loads(line)
                if sample['phase']!='measurement': continue
                loads.append(sample['load_average'][0])
                for row in sample['ps'].splitlines():
                    fields=row.split(None,5)
                    if len(fields)!=6: continue
                    pid,cpu,rss,vsz,cpu_time,command=fields
                    kind='java' if command.endswith('/java') else 'spe' if command.endswith('/spe') else 'k6' if command.endswith('/k6') else command
                    processes.setdefault((kind,pid),[]).append((sample['monotonic'],clock_seconds(cpu_time),int(rss),float(cpu)))
        observation['sampled_processes']=[]
        observation['sampled_load_average_1m']=dict(minimum=min(loads),maximum=max(loads)) if loads else None
        for (kind,pid),samples in processes.items():
            cpu_cores=(samples[-1][1]-samples[0][1])/(samples[-1][0]-samples[0][0]) if len(samples)>1 else None
            observation['sampled_processes'].append(dict(kind=kind,pid=pid,samples=len(samples),
                rss_peak_mib=max(v[2] for v in samples)/1024, cpu_cores_from_sampled_time_delta=cpu_cores,
                note='Sampling spans only portions of a run; not an instantaneous CPU saturation test.'))
        observation['native_stack_samples']=[]
        for profile in block.glob('spe-*.sample.txt'):
            source=profile.read_text()
            main=re.search(r'(\d+) Thread_\d+: Main Thread',source)
            wait=re.search(r'(\d+) multi_wait  \(in libcurl',source)
            if main and wait:
                observation['native_stack_samples'].append(dict(file=profile.name,main_thread_samples=int(main.group(1)),
                    libcurl_multi_wait_samples=int(wait.group(1)),
                    libcurl_multi_wait_percent=100*int(wait.group(1))/int(main.group(1))))
        result['blocks'].append(observation)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('build/validation'))
    args=parser.parse_args()
    report=dict(generated_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        scope='controlled local experiments; fresh JVM blocks are observational units; temporal dependence may remain',
        statistical_significance='not_assessed',experiments=[],identical_code_variability={},diagnostics=[],excluded=[],
        notes=['CV is sample standard deviation / mean across JVM blocks, including temporal drift.',
               'k6 and SPE latency definitions and percentile methods differ; compare effects within each tool.',
               'Per-block percentile summaries are medians of run percentiles, not pooled request percentiles.',
               'Diagnostic runs include profiler and attachment overhead and are excluded from timing comparisons.'])
    aa={}
    for path in sorted(args.root.glob('**/manifest.json')):
        m=json.loads(path.read_text())
        if m.get('diagnostic_only') and m['status']=='complete':
            report['diagnostics'].append(diagnostics(path.parent)); continue
        if m['status']!='complete' or m['scope']=='runner lifecycle verification only':
            report['excluded'].append(dict(directory=str(path.parent),status=m['status'],scope=m['scope']))
            continue
        comparison=path.with_name('comparison.json')
        if not comparison.exists(): continue
        r=json.loads(comparison.read_text())
        if r['status']!='descriptive_comparison': continue
        protocol=m['protocol']; tool=protocol.get('tool','spe')
        case=protocol.get('scenario','existing')
        if protocol.get('same_artifact'): case='same-'+protocol['same_artifact']
        entry=dict(directory=str(path.parent),tool=tool,case=case,same_artifact=m.get('same_artifact_verified'),
            jar_sha256=m['jar_sha256'],metrics=r['metrics'],blocks=r['blocks'],
            protocol=protocol,measured_requests=sum(t['successful_requests'] for t in r['totals'].values()))
        for block in entry['blocks']:
            runs=[json.loads(p.read_text()) for p in sorted((path.parent/f"{block['block']:02d}-{block['variant']}").glob('run-[0-9][0-9].json'))]
            for percentile in ('p50','p95','p99'):
                block[f'median_run_{percentile}_ms']=statistics.median(run['success_latency_ms'][percentile] for run in runs)
        report['experiments'].append(entry)
        if case.startswith('same-') or case=='control':
            group=aa.setdefault(tool+'/'+case,dict(throughput=[],mean_latency=[],median_run_p50_ms=[],median_run_p95_ms=[],median_run_p99_ms=[]))
            for block in r['blocks']:
                group['throughput'].append(block['combined_successful_throughput_rps'])
                group['mean_latency'].append(block['mean_request_latency_ms'])
                for percentile in ('p50','p95','p99'):
                    group[f'median_run_{percentile}_ms'].append(block[f'median_run_{percentile}_ms'])
    for group,data in aa.items():
        report['identical_code_variability'][group]={key:spread(values) for key,values in data.items()}
    report['analysis_source_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report['timing_totals']=dict(experiments=len(report['experiments']),
        jvm_blocks=sum(len(e['blocks']) for e in report['experiments']),
        measured_requests=sum(e['measured_requests'] for e in report['experiments']))
    report['diagnostic_totals']=dict(experiments=len(report['diagnostics']),
        jvm_blocks=sum(len(e['blocks']) for e in report['diagnostics']))
    output=args.root/'analysis.json'; output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    with (args.root/'blocks.csv').open('w',newline='') as stream:
        fields=('experiment','tool','case','block','variant','combined_successful_throughput_rps',
                'mean_request_latency_ms','median_run_p50_ms','median_run_p95_ms','median_run_p99_ms')
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        for experiment in report['experiments']:
            for block in experiment['blocks']:
                writer.writerow(dict(experiment=experiment['directory'],tool=experiment['tool'],case=experiment['case'],
                    **{key:block[key] for key in fields[3:]}))
    print(output)
    for e in report['experiments']:
        print(e['tool'],e['case'],{key:round(value['change_percent'],2) if value['change_percent'] is not None else None for key,value in e['metrics'].items()})


if __name__=='__main__': main()
