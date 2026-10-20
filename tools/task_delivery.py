"""Offline handoff without Git writes; timed commands with durable start records.

Trusted single-writer tool, not a sandbox or secret scanner. Review before transfer.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


def safe(root, rel):
    p = Path(rel)
    if not re.fullmatch(r'[A-Za-z0-9_./-]+', rel) or p.is_absolute() or '..' in p.parts or str(p) != rel or str(p) == '.' or rel.startswith('-') or any(part.lower() == '.git' for part in p.parts):
        raise ValueError('expected a workspace-relative path')
    current = root
    for part in p.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('symlink rejected')
    return current


def git(root, *args):
    return subprocess.check_output(['git', '--no-optional-locks', *args], cwd=root)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    with path.open('x') as f:
        json.dump(value, f, indent=2, sort_keys=True)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())


def folder(root, output, prefix):
    parent = safe(root, output)
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=parent))


def package(root, baseline, files, receipts, output='.task-delivery'):
    started = time.monotonic()
    root = root.resolve()
    if not re.fullmatch(r'[0-9a-f]{40}', baseline):
        raise ValueError('full baseline SHA required')
    if Path(git(root, 'rev-parse', '--show-toplevel').decode().strip()).resolve() != root:
        raise ValueError('exact workspace root required')
    # A delivery of uncommitted work only; commits must be handed off separately.
    if git(root, 'rev-parse', 'HEAD').decode().strip() != baseline:
        raise ValueError('baseline mismatch')
    if not files or len(files) != len(set(files)):
        raise ValueError('explicit unique file allowlist required')
    for rel in files + receipts:
        path = safe(root, rel)
        if path.exists() and not path.is_file():
            raise ValueError('only regular files allowed')
    changed = set(filter(None, git(root, 'diff', '--no-renames', '--name-only', '-z', baseline).decode().split('\0')))
    if not changed.issubset(set(files)):
        raise ValueError('tracked changes outside allowlist')
    tracked = set(filter(None, git(root, 'ls-files', '-z').decode().split('\0')))
    baseline_files = set(filter(None, git(root, 'ls-tree', '-r', '--name-only', '-z', baseline).decode().split('\0')))
    if any(safe(root, rel).exists() for rel in (baseline_files - tracked) & set(files)):
        raise ValueError('index-only deletion is ambiguous; resolve it before delivery')
    tracked.update(baseline_files)
    before = {rel: sha(safe(root, rel).read_bytes()) if safe(root, rel).exists() else None for rel in files}
    patch = git(root, 'diff', '--no-ext-diff', '--no-textconv', '--binary', '--no-renames', '--src-prefix=a/', '--dst-prefix=b/', baseline, '--', *files)
    for rel in sorted(set(files) - tracked):
        if before[rel] is None:
            raise ValueError('missing new file')
        result = subprocess.run(['git', 'diff', '--no-index', '--no-ext-diff', '--no-textconv', '--binary', '--src-prefix=a/', '--dst-prefix=b/', '--', '/dev/null', rel], cwd=root, capture_output=True)
        if result.returncode not in (0, 1):
            raise ValueError('new-file diff failed')
        patch += result.stdout
    payloads = {'changes.patch': patch}
    for i, rel in enumerate(receipts):
        data = safe(root, rel).read_bytes()
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError('receipt must be an object')
        payloads['receipt-{}.json'.format(i)] = data
    after = {rel: sha(safe(root, rel).read_bytes()) if safe(root, rel).exists() else None for rel in files}
    if before != after or git(root, 'rev-parse', 'HEAD').decode().strip() != baseline:
        raise ValueError('tree changed while packaging')
    dest = folder(root, output, 'package-')
    for name, data in payloads.items():
        with (dest / name).open('xb') as f:
            f.write(data)
    write_json(dest / 'manifest.json', {'baseline': baseline, 'files': before,
        'artifacts': {k: sha(v) for k, v in payloads.items()}, 'receipt_sources': receipts,
        'seconds': time.monotonic() - started, 'acceptance': False,
        'note': 'Hash integrity only; inspect receipts, secrets and scope before transfer/application.'})
    return dest


def verify(dest):
    if dest.is_symlink():
        raise ValueError('symlink rejected')
    value = json.loads((dest / 'manifest.json').read_text())
    artifacts = value['artifacts']
    if not isinstance(artifacts, dict) or 'changes.patch' not in artifacts:
        raise ValueError('invalid artifact manifest')
    for name, digest in artifacts.items():
        if '/' in name or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('invalid artifact identity')
        if sha(safe(dest, name).read_bytes()) != digest:
            raise ValueError('artifact hash mismatch')
    return {'integrity': True, 'acceptance': False, 'artifacts': len(artifacts)}


def phase(root, name, command, output='.task-delivery'):
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', name) or not command:
        raise ValueError('phase name and command required')
    dest = folder(root.resolve(), output, name + '-')
    start = time.monotonic_ns()
    write_json(dest / 'start.json', {'phase': name, 'utc': datetime.now(timezone.utc).isoformat(),
        'monotonic_ns': start, 'pid': os.getpid(), 'status': 'started'})
    # Do not persist argv/environment: they may contain credentials. No shell expansion.
    status, code = 'failed_to_start', 127
    try:
        code = subprocess.call(command, cwd=root)
        status = 'finished'
    except KeyboardInterrupt:
        status, code = 'interrupted', 130
    finally:
        end = time.monotonic_ns()
        write_json(dest / 'end.json', {'phase': name, 'utc': datetime.now(timezone.utc).isoformat(),
            'monotonic_ns': end, 'seconds': (end-start)/1e9, 'exit_code': code,
            'status': status, 'acceptance': False})
        print(str(dest))
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('package')
    p.add_argument('--baseline', required=True)
    p.add_argument('--file', action='append', required=True)
    p.add_argument('--receipt', action='append', default=[])
    p.add_argument('--output', default='.task-delivery')
    p = sub.add_parser('phase')
    p.add_argument('name')
    p.add_argument('command', nargs=argparse.REMAINDER)
    p = sub.add_parser('verify')
    p.add_argument('directory')
    args = parser.parse_args()
    try:
        if args.action == 'verify':
            print(json.dumps(verify(safe(Path.cwd().resolve(), args.directory))))
            return 0
        if args.action == 'package':
            print(package(Path.cwd(), args.baseline, args.file, args.receipt, args.output))
            return 0
        command = args.command[1:] if args.command[:1] == ['--'] else args.command
        return phase(Path.cwd(), args.name, command)
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print('Delivery blocked: ' + type(exc).__name__)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
