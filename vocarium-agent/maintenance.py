"""Allowlisted CLI maintenance. No prompts, inference or user supplied commands."""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, HTTPRedirectHandler, build_opener

PACKAGES = {'codex': '@openai/codex', 'claude': '@anthropic-ai/claude-code'}
MODEL_ID = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._:/\[\]-]{0,119}$')


def stamp() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    with build_opener(NoRedirect).open(Request(url, headers=headers or {}), timeout=20) as response:
        data = json.loads(response.read(2_000_000))
    if not isinstance(data, dict):
        raise ValueError('Invalid catalog response')
    return data


def protocol_models(provider: str) -> list[dict[str, str]]:
    """Read CLI initialization/model metadata without submitting a user turn."""
    if provider == 'codex':
        command = ['codex', 'app-server']
        first = {'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'vocarium', 'version': '1.0'}}}
    else:
        command = ['claude', '-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose', '--setting-sources', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}']
        first = {'type': 'control_request', 'request_id': 'catalog', 'request': {'subtype': 'initialize'}}
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    selector = selectors.DefaultSelector()
    try:
        assert proc.stdin and proc.stdout
        selector.register(proc.stdout, selectors.EVENT_READ)
        def send(value: dict[str, Any]) -> None:
            proc.stdin.write((json.dumps(value) + '\n').encode()); proc.stdin.flush()
        send(first)
        end = time.monotonic() + 35
        buffer = b''
        models: list[dict[str, str]] = []
        while time.monotonic() < end:
            if not selector.select(timeout=0.5):
                if proc.poll() is not None:
                    break
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > 2_000_000:
                raise ValueError('Catalog response too large')
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if provider == 'codex':
                    if item.get('id') == 1:
                        if item.get('error'):
                            raise ValueError('CLI initialization failed')
                        send({'method': 'initialized'})
                        send({'id': 2, 'method': 'model/list', 'params': {'limit': 100}})
                    if item.get('id') != 2:
                        continue
                    result = item.get('result') or {}
                    models.extend({'value': m.get('model') or m.get('id'), 'label': m.get('displayName') or m.get('model') or m.get('id')} for m in result.get('data', []))
                    if result.get('nextCursor'):
                        send({'id': 2, 'method': 'model/list', 'params': {'limit': 100, 'cursor': result['nextCursor']}})
                        continue
                    return models
                if item.get('type') == 'control_response':
                    response = (item.get('response') or {}).get('response') or {}
                    return [{'value': m.get('value'), 'label': m.get('displayName') or m.get('value')} for m in response.get('models', [])]
        raise TimeoutError('CLI catalog unavailable')
    finally:
        selector.close()
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        if proc.stdin: proc.stdin.close()
        if proc.stdout: proc.stdout.close()


def run_install(command: list[str]) -> None:
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        code = process.wait(timeout=600)
        if code:
            raise subprocess.CalledProcessError(code, command)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise


class Maintenance:
    def __init__(self, root: Path, run_lock: threading.Lock, credentials: Callable[[], tuple[str, str]], login_busy: Callable[[], bool]) -> None:
        self.root = root
        self.run_lock = run_lock
        self.credentials = credentials
        self.login_busy = login_busy
        self.lock = threading.RLock()
        self.state: dict[str, Any] = {'job': None, 'clis': {}, 'catalogs': {}}
        try:
            self.state.update(json.loads((root / 'status.json').read_text()))
        except (OSError, ValueError):
            pass
        if (self.state.get('job') or {}).get('status') == 'running':
            self.state['job'].update(status='failed', message='Update interrupted by runner restart', finished_at=stamp())
        # Atomic executable symlinks live on the existing persistent agent volume.
        os.environ['PATH'] = str(root / 'bin') + os.pathsep + os.environ.get('PATH', '')

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / 'status.tmp'
        temporary.write_text(json.dumps(self.state))
        temporary.replace(self.root / 'status.json')

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.state))

    def known_models(self, provider: str) -> set[str]:
        with self.lock:
            catalog = self.state['catalogs'].get(provider, {})
            return {m['value'] for m in catalog.get('models', []) + catalog.get('retained_models', [])}

    def start(self, kind: str, provider: str | None = None) -> dict[str, Any]:
        if kind not in {'check', 'models', 'update'} or (kind == 'update' and provider not in PACKAGES):
            raise ValueError('Unsupported maintenance action')
        if not self.run_lock.acquire(blocking=False):
            raise BlockingIOError('An agent run or maintenance task is active. Try again after it finishes.')
        try:
            if self.login_busy():
                raise BlockingIOError('Finish the current CLI login before maintenance.')
            with self.lock:
                self.state['job'] = {'id': uuid.uuid4().hex, 'kind': kind, 'provider': provider, 'status': 'running', 'message': 'Checking versions' if kind == 'check' else 'Refreshing models' if kind == 'models' else 'Installing CLI update', 'started_at': stamp()}
                self.save()
                threading.Thread(target=self.work, args=(kind, provider), daemon=True).start()
                return self.snapshot()
        except Exception:
            self.run_lock.release()
            raise

    def version(self, executable: str) -> str:
        result = subprocess.run([executable, '--version'], capture_output=True, text=True, timeout=15, check=True)
        match = re.search(r'\d+\.\d+\.\d+(?:[-+][\w.-]+)?', result.stdout)
        if not match:
            raise ValueError('CLI version check failed')
        return match[0]

    def check_versions(self) -> None:
        for provider, package in PACKAGES.items():
            entry = dict(self.state['clis'].get(provider, {}))
            entry.update(checked_at=stamp(), error=None)
            try:
                entry['installed'] = self.version(provider)
                latest = fetch_json('https://registry.npmjs.org/' + package + '/latest').get('version', '')
                if not re.fullmatch(r'\d+\.\d+\.\d+', latest):
                    raise ValueError('Invalid release version')
                entry['latest'] = latest
            except Exception:
                entry['error'] = 'Version check unavailable; previous information retained'
            with self.lock:
                self.state['clis'][provider] = entry

    def install(self, provider: str) -> None:
        package = PACKAGES[provider]
        latest = fetch_json('https://registry.npmjs.org/' + package + '/latest')['version']
        if not re.fullmatch(r'\d+\.\d+\.\d+', latest):
            raise ValueError('Invalid release version')
        stage = self.root / 'releases' / (provider + '-' + latest + '-' + uuid.uuid4().hex[:8])
        stage.mkdir(parents=True)
        activated = False
        try:
            # npm only receives fixed package names and a verified version, never shell text.
            run_install(['npm', 'install', '--global', '--prefix', str(stage), '--cache', str(self.root / 'npm-cache'), '--no-audit', '--no-fund', '--registry=https://registry.npmjs.org', package + '@' + latest])
            binary = stage / 'bin' / provider
            if self.version(str(binary)) != latest:
                raise ValueError('Installed CLI did not report requested version')
            bindir = self.root / 'bin'
            bindir.mkdir(exist_ok=True)
            temporary = bindir / (provider + '.next')
            temporary.unlink(missing_ok=True)
            temporary.symlink_to(binary)
            temporary.replace(bindir / provider)
            activated = True
            with self.lock:
                self.state['clis'][provider] = {'installed': latest, 'latest': latest, 'checked_at': stamp(), 'updated_at': stamp(), 'error': None}
                self.state['job']['message'] = 'CLI updated; refreshing model catalogs'
                self.save()
        finally:
            if not activated:
                shutil.rmtree(stage, ignore_errors=True)

    def discover(self, provider: str) -> list[dict[str, str]]:
        if provider != 'zai':
            return protocol_models(provider)
        token, base = self.credentials()
        if not token:
            raise ValueError('Z.AI credentials missing')
        data = fetch_json(base.rstrip('/') + '/v1/models', {'x-api-key': token, 'Authorization': 'Bearer ' + token, 'anthropic-version': '2023-06-01'})
        return [{'value': m['id'], 'label': m.get('display_name') or m['id']} for m in data.get('data', []) if str(m.get('id', '')).startswith('glm-')]

    def refresh(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {provider: pool.submit(self.discover, provider) for provider in ('codex', 'claude', 'zai')}
            for provider, future in futures.items():
                with self.lock:
                    entry = dict(self.state['catalogs'].get(provider, {}))
                entry.update(checked_at=stamp(), error=None)
                try:
                    models = future.result()
                    models = list({m['value']: m for m in models if MODEL_ID.fullmatch(str(m.get('value') or ''))}.values())
                    if not models:
                        raise ValueError('Empty catalog')
                    present = {m['value'] for m in models}
                    retained = [m for m in entry.get('models', []) + entry.get('retained_models', []) if m['value'] not in present]
                    entry['retained_models'] = list({m['value']: m for m in retained}.values())
                    entry.update(models=models, refreshed_at=stamp(), source='CLI' if provider != 'zai' else 'Provider API')
                except Exception:
                    entry['error'] = 'Model catalog unavailable; previous models retained'
                with self.lock:
                    self.state['catalogs'][provider] = entry
                    self.save()

    def work(self, kind: str, provider: str | None) -> None:
        try:
            if kind == 'check': self.check_versions()
            elif kind == 'update':
                assert provider is not None
                self.install(provider)
                self.refresh()
            else: self.refresh()
            with self.lock:
                warnings = any(e.get('error') for e in self.state['clis' if kind == 'check' else 'catalogs'].values())
                self.state['job'].update(status='completed', message='Completed with unavailable catalogs or version checks' if warnings else 'Completed', finished_at=stamp())
        except Exception:
            with self.lock:
                self.state['job'].update(status='failed', message='CLI update failed. The previous executable remains active; check network and free disk space.', finished_at=stamp())
        finally:
            try:
                with self.lock: self.save()
            finally:
                self.run_lock.release()
