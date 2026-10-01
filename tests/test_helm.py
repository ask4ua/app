"""Render both environments without a cluster and check deployment contracts."""
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'deploy/helm/demoapp'
pytestmark = pytest.mark.skipif(not shutil.which('helm'), reason='Helm is required')
DIGEST = 'sha256:' + 'a' * 64


def render(environment, *extra):
    return subprocess.run(['helm', 'template', 'demoapp-' + environment, str(CHART),
        '--namespace', environment, '-f', str(CHART / f'values-{environment}.yaml'),
        '--set', 'image.repository=example.azurecr.io/apps/demoapp',
        '--set', 'image.digest=' + DIGEST, *extra], text=True, capture_output=True)


@pytest.mark.parametrize('environment,replicas', [('staging', 1), ('production', 2)])
def test_environment_contract(environment, replicas):
    subprocess.run(['helm', 'lint', str(CHART), '--strict', '--namespace', environment,
        '-f', str(CHART / f'values-{environment}.yaml'), '--set', 'image.repository=example/demoapp',
        '--set', 'image.digest=' + DIGEST], check=True, capture_output=True)
    result = render(environment)
    assert result.returncode == 0, result.stderr
    objects = list(yaml.safe_load_all(result.stdout))
    assert all(obj['metadata']['namespace'] == environment for obj in objects)
    deployment = next(obj for obj in objects if obj['kind'] == 'Deployment')
    assert deployment['spec']['replicas'] == replicas
    pod = deployment['spec']['template']['spec']
    container = pod['containers'][0]
    assert container['image'] == 'example.azurecr.io/apps/demoapp@' + DIGEST
    env = {item['name']: item for item in container['env']}
    assert env['DATABASE_URL']['valueFrom']['secretKeyRef']['name'] == 'demoapp-runtime'
    assert env['AZURE_STORAGE_CONNECTION_STRING']['valueFrom']['secretKeyRef']['key'] == 'AZURE_STORAGE_CONNECTION_STRING'
    assert env['AZURE_STORAGE_CONTAINER']['value'] == 'demoapp-' + environment
    assert container['readinessProbe']['httpGet']['path'] == '/api/health'
    assert container['livenessProbe']['httpGet']['path'] != '/api/health'
    assert any(obj['kind'] == 'PodDisruptionBudget' for obj in objects) == (environment == 'production')
    service = next(obj for obj in objects if obj['kind'] == 'Service')
    assert service['spec']['selector'] == deployment['spec']['selector']['matchLabels']


def test_namespace_mismatch_rejected():
    result = render('staging', '--namespace', 'production')
    assert result.returncode != 0
    assert 'requires --namespace staging' in result.stderr


def test_ingress_and_tls_render():
    result = render('staging', '--set', 'ingress.enabled=true', '--set', 'ingress.host=demo.example.com',
                    '--set', 'ingress.tls[0].secretName=demo-tls', '--set', 'ingress.tls[0].hosts[0]=demo.example.com')
    assert result.returncode == 0, result.stderr
    ingress = next(obj for obj in yaml.safe_load_all(result.stdout) if obj['kind'] == 'Ingress')
    assert ingress['spec']['tls'][0]['secretName'] == 'demo-tls'
    assert ingress['spec']['rules'][0]['host'] == 'demo.example.com'


def test_packaged_release_contains_digest_and_environment_values(tmp_path):
    subprocess.run([sys.executable, str(ROOT / 'scripts/package_chart.py'), '--version', '0.42.1',
        '--repository', 'example.azurecr.io/apps/demoapp', '--digest', DIGEST,
        '--commit', 'abc123', '--destination', str(tmp_path)], check=True, capture_output=True)
    archive = tmp_path / 'demoapp-0.42.1.tgz'
    with tarfile.open(archive) as tar:
        values = yaml.safe_load(tar.extractfile('demoapp/values.yaml'))
        assert values['image']['digest'] == DIGEST
        assert yaml.safe_load(tar.extractfile('demoapp/Chart.yaml'))['version'] == '0.42.1'
        assert 'demoapp/values-production.yaml' in tar.getnames()
    result = subprocess.run(['helm', 'template', 'demoapp', str(archive), '--namespace', 'staging',
                             '-f', str(CHART / 'values-staging.yaml')], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert '@' + DIGEST in result.stdout


def test_argocd_uses_separate_channels_and_automatic_sync():
    objects = list(yaml.safe_load_all((ROOT / 'deploy/argocd/applications.yaml').read_text()))
    apps = [obj for obj in objects if obj['kind'] == 'Application']
    assert len(apps) == 2
    for app in apps:
        spec = app['spec']
        environment = spec['destination']['namespace']
        assert spec['source']['repoURL'].endswith('/charts/' + environment)
        assert spec['source']['helm']['valueFiles'] == [f'values-{environment}.yaml']
        assert spec['syncPolicy']['automated']['selfHeal'] is True
