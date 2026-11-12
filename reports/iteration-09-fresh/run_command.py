"""Rehearsal-only bounded command recorder. Not part of the restored candidate."""
from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import time

BASE = Path('/workspace/scratch/ee2def0b7c52')
EVIDENCE = Path(__file__).resolve().parent
SOURCE = EVIDENCE.parent / 'source'
TOOLCHAIN = BASE / 'neuro-rust-tools/rustup/toolchains/1.99.1-x86_64-unknown-linux-gnu/bin'

name, limit, *argv = sys.argv[1:]
assert name.replace('-', '').isalnum() and argv
env = os.environ.copy()
declared = {
    'CARGO_NET_OFFLINE': 'true',
    'RUSTUP_HOME': str(BASE / 'neuro-rust-tools/rustup'),
    'RAYON_NUM_THREADS': '2',
    'PATH': str(TOOLCHAIN) + os.pathsep + env.get('PATH', ''),
}
env.update(declared)
start = time.monotonic()
record = {'argv': argv, 'cwd': str(SOURCE), 'timeout_seconds': float(limit),
          'declared_environment': {k:v for k,v in declared.items() if k != 'PATH'},
          'path_prefix': str(TOOLCHAIN), 'status': 'running',
          'cargo_source_cache_reused': str(BASE / 'neuro-rust-tools/cargo'),
          'compiled_target_cache_reused': False, 'network_enabled_for_cargo': False}
record_path = EVIDENCE / (name + '.command.json')
record_path.write_text(json.dumps(record, indent=2) + '\n')
with (EVIDENCE / (name + '.stdout.log')).open('w') as out, (EVIDENCE / (name + '.stderr.log')).open('w') as err:
    proc = subprocess.Popen(argv, cwd=SOURCE, env=env, stdout=out, stderr=err, start_new_session=True)
    try:
        proc.wait(timeout=float(limit))
        record['status'] = 'passed' if proc.returncode == 0 else 'failed'
    except subprocess.TimeoutExpired:
        record['status'] = 'timeout'
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
    record['returncode'] = proc.returncode
record['elapsed_seconds'] = round(time.monotonic() - start, 3)
record_path.write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps({k:record[k] for k in ('argv','status','returncode','elapsed_seconds')}), flush=True)
if record['status'] != 'passed':
    print((EVIDENCE / (name + '.stderr.log')).read_text()[-5000:])
    print((EVIDENCE / (name + '.stdout.log')).read_text()[-5000:])
raise SystemExit(0 if record['status'] == 'passed' else 1)
