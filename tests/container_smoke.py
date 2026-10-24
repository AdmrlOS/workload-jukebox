"""Run inside the ARM container. Null output tests services, not physical audibility."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
sys.path.insert(0, '/app')
import jukebox as j

with tempfile.TemporaryDirectory() as d:
    app=j.Appliance(d)
    configure=app.configure
    real_audio_config=app.audio_config
    def test_configure():
        configure()
    def test_audio_config():
        if app.settings['output'] not in ('default', 'test-null'):
            return real_audio_config()
        Path('/run/jukebox/pulse.pa').write_text(
            'load-module module-native-protocol-unix socket=/run/jukebox/pulse.sock auth-anonymous=1\n'
            'load-module module-null-sink sink_name=office\nset-default-sink office\n'
            'load-module module-role-ducking trigger_roles=phone ducking_roles=music volume=0%\n')
    app.audio_config=test_audio_config
    app.configure=test_configure
    worker=threading.Thread(target=app.loop,daemon=True)
    worker.start()
    def wait_for(predicate,label,timeout=25):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if predicate():print('PASS:',label,flush=True);return
            time.sleep(.2)
        raise AssertionError(label+': '+str(app.status()))
    players=[]
    try:
        wait_for(lambda:app.ready,'all real receiver processes start with a test null output')
        for port in (5000,4070):
            with socket.create_connection(('127.0.0.1',port),timeout=2):pass
            print('PASS: listening port',port,flush=True)
        spotify=app.services['spotify']
        oldpid=spotify.process.pid
        spotify.process.kill()
        wait_for(lambda:spotify.restarts>=1 and spotify.alive() and spotify.process.pid!=oldpid,'Spotify crash recovery')
        for name,role in (('librespot','music'),('Admiral AirPlay','phone')):
            players.append(subprocess.Popen(['pacat','--raw','--rate=44100','--channels=2','--property=application.name='+name,'--property=media.role='+role],stdin=open('/dev/zero','rb')))
        wait_for(lambda:app.active=='airplay','AirPlay priority using real PulseAudio streams')
        wait_for(lambda:any(s['source']=='spotify' and s['waiting'] for s in app.streams),'Spotify muted during AirPlay')
        players[-1].terminate();players[-1].wait()
        wait_for(lambda:app.active=='spotify','Spotify resumes after AirPlay stream closes')
        app.update(dict(name='Test Office',volume=25,limit=60,output='default'))
        wait_for(lambda:all(app.services[s].alive() for s in ('airplay','spotify')),'rename restarts receivers')
        old_audio_pid=app.services['audio'].process.pid
        app.recover('audio')
        assert not app.ready
        wait_for(lambda:app.ready and app.services['audio'].process.pid != old_audio_pid,'audio engine manual recovery')
        app.outputs=[{'id':'test-null','label':'Test output'}]
        app.update(dict(app.settings,output='test-null'))
        wait_for(lambda:app.pending_output is None and app.ready,'valid output opens before committing settings')
        import json
        assert json.loads((Path(d)/'settings.json').read_text())['output']=='test-null'
        print('PASS: verified output persisted',flush=True)
        app.outputs=[{'id':'plughw:CARD=MissingTestCard,DEV=0','label':'Test missing hardware'}]
        fallback=dict(app.settings)
        app.update(dict(fallback,output='plughw:CARD=MissingTestCard,DEV=0'))
        assert app.pending_output is not None
        wait_for(lambda:app.pending_output is None and app.ready and app.settings==fallback,'invalid output automatically restores the previous working settings',timeout=35)
    finally:
        for player in players:
            if player.poll() is None:player.terminate();player.wait()
        app.close();worker.join(timeout=10)
    print('PASS: clean shutdown',flush=True)
