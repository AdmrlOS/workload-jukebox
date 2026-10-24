#!/usr/bin/env python3
"""Local audio appliance. Python stdlib only; Linux audio tools live in the image."""
import argparse
import collections
import hmac
import json
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent

def run(args, timeout=3):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=True)
    return p.stdout

def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(value, f)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def validate_settings(value):
    if not isinstance(value, dict) or set(value) != {'name', 'volume', 'limit', 'output'}:
        raise ValueError('Supply name, volume, limit and output.')
    if not isinstance(value['name'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}', value['name']):
        raise ValueError('Use 1–40 letters, numbers, spaces, dots, dashes or underscores for the name.')
    if any(type(value[k]) is not int for k in ('volume', 'limit')):
        raise ValueError('Volume values must be whole numbers.')
    if not 1 <= value['limit'] <= 100 or not 0 <= value['volume'] <= value['limit']:
        raise ValueError('Volume must be between 0 and the maximum; maximum must be 1–100.')
    if not isinstance(value['output'], str) or not re.fullmatch(r'[A-Za-z0-9_:,=.\-]{1,120}', value['output']):
        raise ValueError('Invalid audio output.')
    return dict(value)

class Service:
    def __init__(self, name, command, log):
        self.name, self.command, self.log = name, command, log
        self.process = None
        self.restarts = 0
        self.failures = 0
        self.next_start = 0
        self.since = 0
        self.last_issue = None
        self.listening = False

    def tick(self, now):
        if self.process is not None and self.process.poll() is not None:
            self.listening = False
            self.failures += 1
            self.restarts += 1
            self.log(self.name, f'Exited with code {self.process.returncode}; automatic recovery scheduled.')
            self.process = None
            self.next_start = now + min(30, 2 ** min(self.failures, 5))
        if self.process is None and now >= self.next_start:
            try:
                self.process = subprocess.Popen(self.command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True, env=dict(os.environ, PULSE_PROP='media.role=' + ('phone' if self.name == 'airplay' else 'music')))
                threading.Thread(target=self.read_errors, args=(self.process,), daemon=True).start()
                self.since = now
                self.log(self.name, 'Started.')
            except OSError as e:
                self.next_start = now + 30
                self.log(self.name, 'Unable to start: ' + str(e))
        if self.process and now - self.since > 60:
            self.failures = 0

    def read_errors(self, process):
        # Classify bounded lines, rather than expose protocol logs or account identifiers.
        issues = {'address already in use': 'A network port is already in use.',
                  'permission denied': 'Permission denied; check audio device access.',
                  'connection refused': 'A required local service refused the connection.',
                  'no such file': 'A required file or audio device is missing.',
                  'failed to connect': 'Connection failed; check network and service health.',
                  'fatal': 'The receiver reported a fatal error.'}
        try:
            while True:
                line = process.stderr.readline(4096)
                if not line:
                    break
                text = line.decode('utf-8', errors='replace').lower()
                for needle, description in issues.items():
                    if needle in text and self.last_issue != description:
                        self.last_issue = description
                        self.log(self.name, description)
                        break
        finally:
            process.stderr.close()

    def stop(self):
        self.listening = False
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        self.process = None
        self.next_start = 0

    def alive(self):
        return self.process is not None and self.process.poll() is None

class Appliance:
    def __init__(self, data, demo=False):
        self.demo = demo
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data, 0o700)
        self.settings = validate_settings({'name': os.environ.get('JUKEBOX_NAME', 'Admiral Office'), 'volume': 40, 'limit': 70, 'output': os.environ.get('ALSA_DEVICE', 'default')})
        if (self.data / 'settings.json').exists():
            self.settings = validate_settings(json.loads((self.data / 'settings.json').read_text()))
        self.lock = threading.RLock()
        self.events = collections.deque(maxlen=100)
        self.started = time.monotonic()
        self.heartbeat = self.started
        self.services = {}
        self.pending_output = None
        self.outputs = []
        self.output_scan_at = 0
        self.audio_ok = False
        self.audio_error = 'Waiting for audio output.'
        self.streams = []
        self.active = None
        self.ready = False
        self.audio_failures = 0
        self.stop_event = threading.Event()
        self.log('system', 'Preview mode; no receivers or speakers attached.' if demo else 'Appliance starting.')

    def log(self, service, message):
        self.events.append({'time': time.strftime('%H:%M:%S', time.gmtime()), 'service': service, 'message': message})
        print(json.dumps({'service': service, 'message': message}), flush=True)

    def configure(self):
        Path('/run/jukebox').mkdir(mode=0o700, exist_ok=True)
        Path('/run/dbus').mkdir(exist_ok=True)
        run(['dbus-uuidgen', '--ensure'])
        # All clients run in the private workload. The socket is in a root-only directory.
        self.audio_config()
        self.receiver_config()
        Path('/run/jukebox/avahi.conf').write_text('[server]\nhost-name=admiral-jukebox\nuse-ipv6=yes\n[wide-area]\nenable-wide-area=no\n[publish]\npublish-workstation=no\n')
        self.services = {
            'dbus': Service('dbus', ['dbus-daemon', '--system', '--nofork', '--nopidfile'], self.log),
            'discovery': Service('discovery', ['avahi-daemon', '--no-drop-root', '--no-chroot', '-f', '/run/jukebox/avahi.conf'], self.log),
            'audio': Service('audio', ['pulseaudio', '--daemonize=no', '--disallow-exit', '--disable-shm', '--exit-idle-time=-1', '--use-pid-file=no', '-nF', '/run/jukebox/pulse.pa'], self.log),
            'airplay': Service('airplay', ['shairport-sync', '-c', '/run/jukebox/shairport.conf'], self.log),
            'spotify': Service('spotify', self.spotify_command(), self.log),
        }

    def audio_config(self):
        device = self.settings['output']
        if not re.fullmatch(r'[A-Za-z0-9_:,=.\-]+', device):
            raise ValueError('Invalid ALSA_DEVICE')
        Path('/run/jukebox/pulse.pa').write_text(
            'load-module module-native-protocol-unix socket=/run/jukebox/pulse.sock auth-anonymous=1\n'
            f'load-module module-alsa-sink device={device} sink_name=office\n'
            'set-default-sink office\n'
            'load-module module-role-ducking trigger_roles=phone ducking_roles=music volume=0%\n')
    def scan_outputs(self):
        try:
            text = run(['aplay', '-l'])
            outputs = [{'id': 'default', 'label': 'System default'}]
            for line in text.splitlines():
                match = re.match(r'card (\d+): ([A-Za-z0-9_]+) \[([^\]]+)\], device (\d+): ([^\[]+)', line)
                if match:
                    _, card, label, device, description = match.groups()
                    outputs.append({'id': f'plughw:CARD={card},DEV={device}', 'label': label + ' · ' + description.strip()})
            self.outputs = outputs
        except (OSError, subprocess.SubprocessError):
            self.outputs = []

    def restart_audio(self):
        self.ready = False
        self.audio_ok = False
        self.active, self.streams = None, []
        for name in ('airplay', 'spotify', 'audio'):
            self.services[name].stop()
        self.audio_config()

    def rollback_output(self):
        self.settings = self.pending_output['previous']
        self.pending_output = None
        self.audio_failures = 0
        self.receiver_config()
        self.services['spotify'].command = self.spotify_command()
        self.restart_audio()
        self.log('audio', 'New output could not be opened. Previous settings restored automatically.')

    def receiver_config(self):
        # Debian's Shairport Sync provides classic AirPlay; no AirPlay 2 claim.
        Path('/run/jukebox/shairport.conf').write_text(
            'general = { name = ' + json.dumps(self.settings['name']) + '; output_backend = "pa"; };\n'
            'pa = { application_name = "Admiral AirPlay"; };\n')

    def spotify_command(self):
        return ['librespot', '--name', self.settings['name'], '--backend', 'pulseaudio', '--device-type', 'speaker', '--bitrate', '320', '--initial-volume', '100', '--disable-audio-cache', '--system-cache', str(self.data / 'spotify'), '--zeroconf-port', '4070']

    def probe_audio(self):
        sinks = json.loads(run(['pactl', '-f', 'json', 'list', 'sinks']))
        if not any(s['name'] == 'office' for s in sinks):
            raise RuntimeError('Configured audio output is unavailable. Check the DAC and ALSA_DEVICE.')
        run(['pactl', 'set-sink-volume', 'office', str(self.settings['volume']) + '%'])
        inputs = json.loads(run(['pactl', '-f', 'json', 'list', 'sink-inputs']))
        streams = []
        for item in inputs:
            props = item.get('properties', {})
            binary = props.get('application.process.binary', '').lower()
            app = props.get('application.name', '').lower()
            source = 'airplay' if 'shairport' in binary or 'airplay' in app else 'spotify' if 'librespot' in binary or 'librespot' in app else None
            if source:
                streams.append({'source': source, 'id': item['index'], 'streaming': not item.get('corked', True)})
        # Native role ducking applies in the audio thread; this is a recovery fallback.
        airplay = any(s['source'] == 'airplay' and s['streaming'] for s in streams)
        for item in streams:
            mute = airplay and item['source'] == 'spotify'
            run(['pactl', 'set-sink-input-mute', str(item['id']), '1' if mute else '0'])
            item['waiting'] = mute
        self.streams = streams
        self.active = 'airplay' if airplay else 'spotify' if any(s['source'] == 'spotify' and s['streaming'] for s in streams) else None

    def loop(self):
        try:
            if not self.demo:
                self.configure()
            while not self.stop_event.is_set():
                with self.lock:
                    now = time.monotonic()
                    if not self.demo:
                        if now >= self.output_scan_at:
                            self.scan_outputs()
                            self.output_scan_at = now + 10
                        if self.pending_output and now >= self.pending_output['deadline']:
                            self.rollback_output()
                        self.services['dbus'].tick(now)
                        self.services['audio'].tick(now)
                        if Path('/run/dbus/system_bus_socket').exists():
                            self.services['discovery'].tick(now)
                        try:
                            self.probe_audio()
                            self.audio_ok, self.audio_error = True, None
                            self.audio_failures = 0
                            if self.pending_output:
                                atomic_json(self.data / 'settings.json', self.settings)
                                self.pending_output = None
                                self.log('audio', 'New output opened successfully; settings saved.')
                        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as e:
                            self.audio_ok, self.audio_error = False, str(e)[:180]
                            self.active, self.streams = None, []
                            self.audio_failures += 1
                            if self.audio_failures == 3:
                                self.log('audio', 'Output check failed; restarting the audio engine.')
                                self.services['audio'].stop()
                            if self.audio_failures >= 30:
                                # Admiral ignores Docker HEALTHCHECK; exit PID 1 for its supervisor.
                                self.log('watchdog', 'Audio failed for 30 checks; requesting workload restart.')
                                os._exit(1)
                        if self.audio_ok and self.services['discovery'].alive():
                            self.services['airplay'].tick(now)
                            self.services['spotify'].tick(now)
                        if any(s.failures >= 5 for s in self.services.values()):
                            self.log('watchdog', 'Repeated service failure; requesting workload restart.')
                            os._exit(1)
                        for name, port in (('airplay', 5000), ('spotify', 4070)):
                            try:
                                with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                                    self.services[name].listening = True
                            except OSError:
                                self.services[name].listening = False
                        self.ready = self.audio_ok and all(s.alive() for s in self.services.values()) and all(self.services[n].listening for n in ('airplay', 'spotify'))
                    self.heartbeat = time.monotonic()
                self.stop_event.wait(1)
        except Exception as e:
            self.log('fatal', str(e))
            if not self.demo:
                os._exit(1)

    def status(self):
        with self.lock:
            return {'demo': self.demo, 'settings': dict(self.settings), 'outputs': list(self.outputs), 'output_pending': self.pending_output is not None, 'ready': self.ready,
                    'audio_ok': self.audio_ok, 'audio_error': self.audio_error,
                    'active': self.active, 'streams': self.streams,
                    'uptime': int(time.monotonic() - self.started),
                    'watchdog_ok': time.monotonic() - self.heartbeat < 15,
                    'services': {k: {'running': v.alive(), 'restarts': v.restarts, 'issue': v.last_issue, 'listening': v.listening} for k, v in self.services.items()},
                    'events': list(self.events)}

    def update(self, value):
        value = validate_settings(value)
        with self.lock:
            if self.pending_output:
                raise ValueError('Wait for the audio output check to finish before saving again.')
            renamed = value['name'] != self.settings['name']
            changed_output = value['output'] != self.settings['output']
            if changed_output and value['output'] not in {o['id'] for o in self.outputs}:
                raise ValueError('Output is not connected. Refresh the detected outputs and try again.')
            if not self.demo and not changed_output:
                run(['pactl', 'set-sink-volume', 'office', str(value['volume']) + '%'])
            previous = self.settings
            if not changed_output:
                atomic_json(self.data / 'settings.json', value)
            else:
                # Persist the fallback before trying new hardware, including on first boot.
                atomic_json(self.data / 'settings.json', previous)
                self.pending_output = {'previous': previous, 'deadline': time.monotonic() + 15}
            self.settings = value
            if not self.demo:
                self.receiver_config()
                self.services['spotify'].command = self.spotify_command()
                if changed_output:
                    self.restart_audio()
                elif renamed:
                    self.ready = False
                    self.services['airplay'].stop()
                    self.services['spotify'].stop()
            self.log('settings', 'Testing audio output; previous settings retained until it opens.' if changed_output else 'Settings saved.' + (' Receivers restarting after rename.' if renamed else ''))

    def recover(self, target):
        if target not in ('airplay', 'spotify', 'audio'):
            raise ValueError('Choose AirPlay, Spotify or audio.')
        with self.lock:
            if not self.demo:
                self.ready = False
                if target == 'audio':
                    self.restart_audio()
                else:
                    self.services[target].stop()
            self.log(target, 'Manual recovery requested.')

    def close(self):
        self.stop_event.set()
        with self.lock:
            for service in reversed(list(self.services.values())):
                service.stop()

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # Avoid reverse DNS at boot; music control must work without DNS.
        socketserver.TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, status, content, kind='application/json'):
        if kind == 'application/json':
            content = json.dumps(content).encode()
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ('/api/status', '/healthz', '/readyz'):
            state = self.server.app.status()
            if path == '/api/status':
                return self.send(200, state)
            good = state['watchdog_ok'] if path == '/healthz' else state['ready']
            return self.send(200 if good else 503, {'ok': good, 'demo': state['demo']})
        if path == '/api/diagnostics':
            if not self.authorized():
                return self.send(401, {'error': 'Enter the administrator key first.'})
            state = self.server.app.status()
            # No credentials, raw protocol logs, or user identifiers in this bundle.
            return self.send(200, state)
        files = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}
        if path.startswith('/assets/') and Path(path).name in {p.name for p in (ROOT / 'web/assets').iterdir()}:
            import mimetypes
            files[path] = ('assets/' + Path(path).name, mimetypes.guess_type(path)[0] or 'application/octet-stream')
        if path in files:
            name, kind = files[path]
            return self.send(200, (ROOT / 'web' / name).read_bytes(), kind)
        self.send(404, {'error': 'Not found'})

    def authorized(self):
        token = self.server.token
        return bool(token) and hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token)

    def do_POST(self):
        # Deny cross-origin browser mutations, even with a valid key.
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc != self.headers.get('Host'):
            return self.send(403, {'error': 'Cross-origin request refused.'})
        if not self.authorized():
            return self.send(401, {'error': 'Enter the administrator key first.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise ValueError('Invalid request size.')
            self.connection.settimeout(5)
            value = json.loads(self.rfile.read(length))
            path = urlsplit(self.path).path
            if path == '/api/settings':
                self.server.app.update(value)
            elif path == '/api/recover':
                self.server.app.recover(value.get('target'))
            else:
                return self.send(404, {'error': 'Not found'})
            self.send(200, {'ok': True})
        except (ValueError, AttributeError, TypeError) as e:
            self.send(400, {'error': str(e)})
        except (OSError, subprocess.SubprocessError) as e:
            self.send(503, {'error': 'Audio control unavailable. Check diagnostics before retrying.'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', 8080)))
    parser.add_argument('--data', default=os.environ.get('DATA_DIR', '/data'))
    args = parser.parse_args()
    token = os.environ.get('ADMIN_TOKEN', '')
    if not args.demo and len(token) < 20:
        raise SystemExit('Set ADMIN_TOKEN to at least 20 characters before starting.')
    app = Appliance(args.data, args.demo)
    server = Server(('127.0.0.1' if args.demo else '0.0.0.0', args.port), Handler)
    server.app, server.token = app, token
    worker = threading.Thread(target=app.loop, daemon=True)
    worker.start()
    def watchdog():
        while not app.stop_event.wait(5):
            if time.monotonic() - app.heartbeat > 45:
                app.log('watchdog', 'Supervisor stalled; requesting workload restart.')
                os._exit(1)
    threading.Thread(target=watchdog, daemon=True).start()
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f'Admiral Jukebox: http://localhost:{args.port}', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        app.close()

if __name__ == '__main__':
    main()
