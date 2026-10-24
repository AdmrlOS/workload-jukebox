'use strict';
const $ = id => document.getElementById(id);
let state, edited = false, busy = false, pendingTarget;
const key = () => $('admin-key').value;
function message(text) { $('message').textContent = text; $('message').hidden = false; setTimeout(() => $('message').hidden = true, 7000); }
function status(id, text, kind) { const el = $(id); el.textContent = text; el.className = 'status ' + kind; }
function element(tag, text, cls) { const el = document.createElement(tag); el.textContent = text; if (cls) el.className = cls; return el; }
function render(data) {
  state = data;
  $('preview').hidden = !data.demo;
  $('device-name').textContent = data.settings.name;
  document.querySelectorAll('.receiver-name').forEach(el => el.textContent = data.settings.name);
  status('header-status', data.demo ? 'Preview / hardware offline' : data.ready ? 'Receivers running' : 'Needs attention', data.demo ? '' : data.ready ? 'ok' : 'warn');
  for (const source of ['airplay', 'spotify']) {
    const waiting = data.streams.some(s => s.source === source && s.waiting);
    status(source + '-status', data.demo ? 'Not connected' : waiting ? 'Waiting for AirPlay' : data.active === source ? 'Streaming' : data.services[source]?.listening ? 'Receiver listening' : data.services[source]?.running ? 'Starting' : 'Unavailable', data.demo ? '' : waiting ? 'warn' : data.services[source]?.listening ? 'ok' : 'warn');
  }
  $('play-state').textContent = data.demo ? 'Waiting for a real device' : data.active ? 'Audio stream active' : data.ready ? 'Ready for your next selection' : 'Check system health';
  $('now-title').textContent = data.active === 'airplay' ? 'The floor is yours.' : data.active === 'spotify' ? 'An office soundtrack.' : 'A little room for good music.';
  $('now-detail').textContent = data.demo ? 'This is the local interface preview. Connect the ARM jukebox to enable AirPlay and Spotify.' : data.active ? 'Streaming via ' + (data.active === 'airplay' ? 'AirPlay' : 'Spotify Connect') + '. Control the music in your connected app.' : data.ready ? 'Choose this speaker in AirPlay or Spotify Connect. No extra app to install.' : 'A receiver or audio output needs attention. Open System health for the next step.';
  $('source').textContent = 'SOURCE / ' + (data.active || 'NONE').toUpperCase();
  $('output').textContent = 'OUTPUT / ' + (data.audio_ok ? data.settings.volume + '% MASTER' : 'UNAVAILABLE');
  const checks = $('checks'); checks.replaceChildren();
  const rows = [
    ['Audio output', data.audio_ok, data.demo ? 'No hardware attached to preview.' : data.audio_error || 'Configured ALSA output detected. Speaker audibility needs a listening check.'],
    ['Music receivers', !!(data.services.airplay?.listening && data.services.spotify?.listening), 'AirPlay + Spotify Connect. Local ports checked. Discovery from a phone needs a network test.'],
    ['Automatic recovery', !data.demo && data.watchdog_ok, data.demo ? 'Enabled on the deployed appliance.' : 'Watchdog responding · uptime ' + Math.floor(data.uptime / 60) + ' min · ' + Object.values(data.services).reduce((a,s) => a + s.restarts,0) + ' automatic restarts.']
  ];
  for (const [title,ok,detail] of rows) { const box = element('article','','check'); box.append(element('h3',title),element('span',ok?'Check passed':data.demo?'Not connected':'Needs attention','status '+(ok?'ok':data.demo?'':'warn')),element('p',detail)); checks.append(box); }
  $('events').replaceChildren();
  [...data.events].reverse().forEach(e => { const row=element('div','','event'); row.append(element('span',e.time),element('span',e.service),element('span',e.message)); $('events').append(row); });
  $('output-check').textContent = data.output_pending ? 'Checking new output… Previous settings will return if it fails.' : data.audio_ok ? 'Output opened successfully. Check the speakers are audible.' : 'Choose a detected output. Changes are saved only after it opens.';
  const selectedOutput = edited ? $('audio-output').value : data.settings.output;
  const outputs = [...data.outputs];
  if (!outputs.some(o=>o.id===selectedOutput)) outputs.push({id:selectedOutput,label:data.demo?'System default · no hardware in preview':'Current selection (not detected): '+selectedOutput});
  const signature = JSON.stringify(outputs);
  if ($('audio-output').dataset.options !== signature) {
    $('audio-output').replaceChildren(...outputs.map(o=>{const option=element('option',o.label);option.value=o.id;return option;}));
    $('audio-output').dataset.options=signature;
  }
  $('audio-output').value=selectedOutput;
  if (!edited) { $('name').value=data.settings.name; $('limit').value=data.settings.limit; $('volume').max=data.settings.limit; $('volume').value=data.settings.volume; $('volume-label').textContent=data.settings.volume+'%'; }
}
async function refresh() {
  try { const r = await fetch('/api/status',{signal:AbortSignal.timeout(5000)}); if(!r.ok) throw Error(); render(await r.json()); $('offline').hidden=true; }
  catch { $('offline').hidden=false; status('header-status','Connection lost','down'); }
}
async function action(path, value) {
  if (busy) return false;
  if (!key()) { switchTab('settings'); $('admin-key').focus(); message('Enter the administrator key to continue.'); return false; }
  busy=true;
  try { const r = await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+key()},body:JSON.stringify(value),signal:AbortSignal.timeout(15000)}); const result=await r.json(); if(!r.ok) throw Error(result.error || 'Request failed.'); return true; }
  catch(e) { message(e.message || 'Connection lost. Check system health before retrying.'); return false; }
  finally {busy=false;}
}
function switchTab(id) { document.querySelectorAll('.tab').forEach(el=>el.hidden=el.id!==id); document.querySelectorAll('nav button').forEach(el=>el.setAttribute('aria-pressed',String(el.dataset.tab===id))); }
document.querySelectorAll('[data-tab]').forEach(el=>el.addEventListener('click',()=>switchTab(el.dataset.tab)));
$('refresh').addEventListener('click',refresh);
$('settings-form').addEventListener('input', e=>{if(e.target.id!=='admin-key') edited=true;});
$('volume').addEventListener('input',()=> $('volume-label').textContent=$('volume').value+'%');
$('limit').addEventListener('input',()=>{ const limit=Math.max(1,Math.min(100,Number($('limit').value)||1)); $('volume').max=limit; $('volume-label').textContent=$('volume').value+'%'; });
$('settings-form').addEventListener('submit',async e=> { e.preventDefault(); if(await action('/api/settings',{name:$('name').value.trim(),volume:Number($('volume').value),limit:Number($('limit').value),output:$('audio-output').value})){edited=false;message(state?.demo?'Preview settings saved. No hardware was changed.':'Settings accepted. A changed audio output is checked before saving.');await refresh();} });
document.querySelectorAll('[data-recover]').forEach(el=>el.addEventListener('click',()=>{ pendingTarget=el.dataset.recover; $('confirm-title').textContent=el.textContent.replace(' ↻','')+'?'; $('confirm').showModal(); }));
$('confirm').addEventListener('close',async()=>{if($('confirm').returnValue==='restart' && await action('/api/recover',{target:pendingTarget})){message(state?.demo?'Preview only. No receiver was restarted.':'Recovery requested. Check status as the receiver starts.');await refresh();}});
$('download').addEventListener('click',async()=>{if(!key()){switchTab('settings');$('admin-key').focus();return message('Enter the administrator key first.');} try { const r=await fetch('/api/diagnostics',{headers:{Authorization:'Bearer '+key()},signal:AbortSignal.timeout(5000)}); if(!r.ok)throw Error('Unable to download. Check the administrator key.'); const url=URL.createObjectURL(await r.blob()); const a=document.createElement('a'); a.href=url;a.download='admiral-jukebox-diagnostics.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000); }catch(e){message(e.message);} });
async function poll(){await refresh();setTimeout(poll,2000);}poll();
