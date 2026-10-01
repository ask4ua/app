"""Build a release chart containing the exact image digest produced by CI."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import yaml

parser = argparse.ArgumentParser()
parser.add_argument('--version', required=True)
parser.add_argument('--repository', required=True)
parser.add_argument('--digest', required=True)
parser.add_argument('--commit', required=True)
parser.add_argument('--destination', default='dist')
args = parser.parse_args()
if not re.fullmatch(r'0\.[1-9][0-9]*\.[1-9][0-9]*', args.version):
    parser.error('Version must be 0.<GitHub run number>.<run attempt>')
if not re.fullmatch(r'sha256:[a-f0-9]{64}', args.digest):
    parser.error('An immutable sha256 image digest is required')
destination = Path(args.destination).resolve()
destination.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory() as temp:
    chart = Path(temp) / 'demoapp'
    shutil.copytree(Path(__file__).resolve().parents[1] / 'deploy/helm/demoapp', chart)
    values_file = chart / 'values.yaml'
    values = yaml.safe_load(values_file.read_text())
    values['image'].update(repository=args.repository, digest=args.digest, tag='')
    values_file.write_text(yaml.safe_dump(values, sort_keys=False))
    subprocess.run(['helm', 'package', str(chart), '--version', args.version,
                    '--app-version', args.commit, '--destination', str(destination)], check=True)
