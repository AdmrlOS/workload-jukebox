import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

spec = importlib.util.spec_from_file_location('jukebox', Path(__file__).resolve().parents[1] / 'jukebox.py')
j = importlib.util.module_from_spec(spec)
spec.loader.exec_module(j)

class SettingsTests(unittest.TestCase):
    def test_rejects_injection_and_invalid_volume(self):
        for value in [dict(name='bad"; shell',volume=10,limit=70,output='default'),dict(name='Office',volume=80,limit=70,output='default'),dict(name='Office',volume=True,limit=70,output='default'),dict(name='../file',volume=10,limit=70,output='default'),dict(name='Office',volume=10,limit=101,output='default')]:
            with self.assertRaises(ValueError): j.validate_settings(value)

    def test_persist_and_reload(self):
        with tempfile.TemporaryDirectory() as d:
            app=j.Appliance(d,True)
            app.update(dict(name='Office Two',volume=23,limit=55,output='default'))
            self.assertEqual(j.Appliance(d,True).settings,app.settings)
            self.assertEqual((Path(d)/'settings.json').stat().st_mode & 0o777,0o600)

    def test_failed_audio_change_does_not_save(self):
        with tempfile.TemporaryDirectory() as d:
            app=j.Appliance(d)
            with patch.object(j,'run',side_effect=OSError('disconnected')):
                with self.assertRaises(OSError):app.update(dict(name='Office',volume=10,limit=20,output='default'))
            self.assertEqual(app.settings['volume'],40)
            self.assertFalse((Path(d)/'settings.json').exists())

    def test_output_discovery_uses_card_names(self):
        with tempfile.TemporaryDirectory() as d:
            app=j.Appliance(d)
            with patch.object(j,'run',return_value='card 2: Headphones [bcm2835 Headphones], device 0: bcm2835 Headphones [bcm2835 Headphones]\n'):
                app.scan_outputs()
            self.assertEqual(app.outputs[1]['id'],'plughw:CARD=Headphones,DEV=0')

    def test_failed_output_rolls_back_and_keeps_durable_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            app=j.Appliance(d)
            old=dict(app.settings)
            app.outputs=[{'id':'plughw:CARD=Headphones,DEV=0','label':'Analogue'}]
            app.services={'spotify':j.Service('spotify',[],app.log)}
            with patch.object(app,'restart_audio'),patch.object(app,'receiver_config'):
                app.update(dict(name='New Office',volume=30,limit=60,output='plughw:CARD=Headphones,DEV=0'))
                self.assertIsNotNone(app.pending_output)
                self.assertEqual(json.loads((Path(d)/'settings.json').read_text()),old)
                app.rollback_output()
                self.assertEqual(app.settings,old)
                self.assertIsNone(app.pending_output)


class AudioTests(unittest.TestCase):
    def test_airplay_priority_and_spotify_resume(self):
        with tempfile.TemporaryDirectory() as d:
            app=j.Appliance(d)
            inputs=[dict(index=1,corked=False,properties={'application.process.binary':'librespot'}),dict(index=2,corked=False,properties={'application.name':'Admiral AirPlay'})]
            calls=[]
            def fake(args,**kw):
                calls.append(args)
                if args[-1]=='sinks':return json.dumps([{'name':'office'}])
                if args[-1]=='sink-inputs':return json.dumps(inputs)
                return ''
            with patch.object(j,'run',side_effect=fake):
                app.probe_audio()
                self.assertEqual(app.active,'airplay')
                self.assertIn(['pactl','set-sink-input-mute','1','1'],calls)
                inputs.pop()
                app.probe_audio()
                self.assertEqual(app.active,'spotify')
                self.assertIn(['pactl','set-sink-input-mute','1','0'],calls)

    def test_no_output_never_reports_ready(self):
        with tempfile.TemporaryDirectory() as d:
            app=j.Appliance(d)
            with patch.object(j,'run',return_value='[]'):
                with self.assertRaises(RuntimeError):app.probe_audio()
            self.assertFalse(app.status()['ready'])

    def test_service_restart_backoff_and_shutdown(self):
        events=[]
        service=j.Service('test',['/bin/sh','-c','exit 2'],lambda *v:events.append(v))
        service.tick(100)
        service.process.wait(timeout=2)
        service.tick(101)
        self.assertIsNone(service.process)
        self.assertEqual(service.next_start,103)
        self.assertEqual(service.restarts,1)
        service.tick(102)
        self.assertIsNone(service.process)
        service.tick(103)
        self.assertIsNotNone(service.process)
        service.stop()
        self.assertIsNone(service.process)

class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=j.Appliance(self.tmp.name,True)
        self.server=j.Server(('127.0.0.1',0),j.Handler)
        self.server.app=self.app
        self.server.token='test-administrator-key'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def request(self,path,value=None,token=None,origin=None):
        headers={}
        if token:headers['Authorization']='Bearer '+token
        if origin:headers['Origin']=origin
        req=Request(self.base+path,data=json.dumps(value).encode() if value is not None else None,headers=headers)
        try:
            with urlopen(req,timeout=2) as r:return r.status,r.read(),r.headers
        except HTTPError as e:
            result=(e.code,e.read(),e.headers)
            e.close()
            return result
    def test_readiness_not_faked_in_preview(self):
        self.assertEqual(self.request('/healthz')[0],200)
        self.assertEqual(self.request('/readyz')[0],503)
        self.assertFalse(json.loads(self.request('/api/status')[1])['ready'])
    def test_auth_origin_and_valid_settings(self):
        value=dict(name='Office',volume=10,limit=60,output='default')
        self.assertEqual(self.request('/api/settings',value)[0],401)
        self.assertEqual(self.request('/api/settings',value,'test-administrator-key','https://evil.example')[0],403)
        self.assertEqual(self.request('/api/settings',value,'test-administrator-key')[0],200)
        self.assertEqual(self.app.settings['volume'],10)
        self.assertEqual(self.request('/api/diagnostics')[0],401)
    def test_static_traversal_headers_and_recovery_validation(self):
        self.assertEqual(self.request('/assets/../jukebox.py')[0],404)
        code,body,headers=self.request('/')
        self.assertEqual(code,200)
        self.assertIn('frame-ancestors',headers['Content-Security-Policy'])
        self.assertEqual(self.request('/api/recover',{'target':'shell'},'test-administrator-key')[0],400)
        self.assertEqual(self.request('/api/recover',{'target':'airplay'},'test-administrator-key')[0],200)

if __name__=='__main__':unittest.main()
