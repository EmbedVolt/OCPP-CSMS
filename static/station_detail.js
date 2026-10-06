const $=s=>document.querySelector(s);
const $$=s=>[...document.querySelectorAll(s)];
const identity=decodeURIComponent(location.pathname.split('/').filter(Boolean).pop()||'');
const state={station:null,messages:[],transactions:[],metrics:null,settings:null,actions:new Set(),testCases:[],pendingCommand:null,refreshing:false,responseRules:[]};
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function pretty(v){return JSON.stringify(v,null,2)}
function table(headers,rows){if(!rows.length)return '<div class="empty">No data yet.</div>';return `<div class="table-wrap"><table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`}
async function api(url,opts={}){const r=await fetch(url,{headers:{'Content-Type':'application/json',...(opts.headers||{})},...opts});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||`${r.status} ${r.statusText}`);return d}
function toast(msg){const e=$('#toast');e.textContent=msg;e.classList.add('show');clearTimeout(window._toast);window._toast=setTimeout(()=>e.classList.remove('show'),3200)}
function fmt(v){return v===null||v===undefined||v===''?'—':v}
function shortTime(v){if(!v)return '—';try{return new Date(v).toLocaleString()}catch{return v}}
function connectorStatusClass(v){const x=String(v||'unknown').toLowerCase();if(x.includes('available'))return 'available';if(x.includes('charging')||x.includes('occupied')||x.includes('preparing')||x.includes('suspend'))return 'charging';if(x.includes('fault'))return 'faulted';if(x.includes('unavailable')||x.includes('reserved'))return 'unavailable';return 'unknown'}
function isConnected(){return !!state.station?.connected}

async function loadStation(){state.station=await api(`/api/stations/${encodeURIComponent(identity)}`);renderStation()}
async function loadMessages(){state.messages=await api(`/api/messages?station_id=${encodeURIComponent(identity)}&limit=500`);renderTraffic()}
async function loadTransactions(){const all=await api('/api/transactions?limit=2000');state.transactions=all.filter(t=>t.station_id===identity);renderTransactions()}
async function loadMetrics(){state.metrics=await api(`/api/stations/${encodeURIComponent(identity)}/dashboard`);renderMetrics()}
async function loadSettings(){state.settings=await api(`/api/stations/${encodeURIComponent(identity)}/settings`);renderSettings()}
async function loadActions(){if(!state.station)return;const rows=await api(`/api/actions/${encodeURIComponent(state.station.protocol)}`);state.actions=new Set(rows.map(x=>x.action));renderActionAvailability()}
async function loadTestCases(){if(!state.station)return;state.testCases=await api(`/api/test-cases/${encodeURIComponent(state.station.protocol)}`);renderTestCases()}
async function loadResponseRules(){state.responseRules=await api('/api/response-rules');renderAuthorizeRule()}

function renderStation(){
  const s=state.station;if(!s)return;
  document.title=`${s.identity} · OCPP Lab`;$('#stationIdentity').textContent=s.identity;$('#crumbIdentity').textContent=s.identity;
  $('#stationState').textContent=s.connected?'Online':'Offline';$('#stationState').className=`status ${s.connected?'online':'offline'}`;$('#protocolBadge').textContent=`OCPP ${s.protocol}`;
  $('#stationSubtitle').textContent=`${s.vendor||'Unknown vendor'} · ${s.model||'Unknown model'} · firmware ${s.firmware||'unknown'}`;
  $('#connectionValue').textContent=s.connected?'ONLINE':'OFFLINE';$('#connectionMeta').textContent=s.connected?`since ${shortTime(s.connected_at)}`:`last seen ${shortTime(s.last_seen)}`;
  $('#infoIdentity').textContent=fmt(s.identity);$('#infoVendor').textContent=fmt(s.vendor);$('#infoModel').textContent=fmt(s.model);$('#infoFirmware').textContent=fmt(s.firmware);$('#infoProtocol').textContent=fmt(s.protocol);$('#infoRemote').textContent=fmt(s.remote_addr);$('#infoConnectedAt').textContent=shortTime(s.connected_at);$('#infoLastSeen').textContent=shortTime(s.last_seen);$('#bootReason').textContent=s.boot_reason?`Boot: ${s.boot_reason}`:'boot reason unknown';
  $('#commandConnectionHint').textContent=s.connected?'connected · commands enabled':'station offline · commands disabled';$('#disconnectBtn').disabled=!s.connected;
  const connectors=Object.entries(s.connectors||{});$('#connectorCards').innerHTML=connectors.length?connectors.map(([k,v])=>`<div class="connector-card"><div class="connector-top"><span class="connector-id">${s.protocol==='1.6'?'Connector':'EVSE : Connector'} ${esc(k)}</span><span class="badge">OCPP ${esc(s.protocol)}</span></div><div class="connector-state ${connectorStatusClass(v)}">${esc(v)}</div><small class="muted">Latest StatusNotification</small></div>`).join(''):'<div class="empty">No StatusNotification received yet.</div>';
  if(s.protocol==='1.6'){$('#config16').classList.remove('hidden');$('#config2').classList.add('hidden');$('#configurationHelp').textContent='OCPP 1.6: GetConfiguration / ChangeConfiguration.'}
  else{$('#config2').classList.remove('hidden');$('#config16').classList.add('hidden');$('#configurationHelp').textContent=`OCPP ${s.protocol}: GetVariables / SetVariables and Device Model reports.`}
  $('#testProtocol').textContent=`OCPP ${s.protocol}`;$('#diagnosticProtocol').textContent=`OCPP ${s.protocol}`;$('#ocppIdentity').value=s.identity;$('#stationProtocol').value=s.protocol;
}

function renderMetrics(){
  const m=state.metrics||{};$('#deliveredEnergy').textContent=`${Number(m.deliveredEnergyKWh||0).toFixed(3)} kWh`;$('#transactionCount').textContent=m.transactionCount||0;$('#transactionMeta').textContent=`${m.activeTransactions||0} active`;
  renderMeterChart();renderOccupancy();renderEvents();
}
function renderMeterChart(){
  const metric=$('#chartMetric').value;const unit={current_a:'A',power_kw:'kW',energy_kwh:'kWh'}[metric];const hours=Number($('#chartWindow').value||24),cut=Date.now()-hours*3600000;const pts=(state.metrics?.meterSeries||[]).filter(x=>Number.isFinite(x[metric])&&(!x.ts||new Date(x.ts).getTime()>=cut)).slice(-120);
  if(!pts.length){$('#meterChart').innerHTML='<div class="empty">No matching meter samples received yet.</div>';return}
  const vals=pts.map(x=>x[metric]),min=Math.min(...vals),max=Math.max(...vals),span=max-min||1,w=900,h=220,pad=28;
  const poly=pts.map((x,i)=>`${pad+(i/(Math.max(1,pts.length-1)))*(w-pad*2)},${h-pad-((x[metric]-min)/span)*(h-pad*2)}`).join(' ');
  const latest=vals[vals.length-1];$('#meterChart').innerHTML=`<div class="chart-value">${latest.toFixed(3)} ${unit}</div><svg viewBox="0 0 ${w} ${h}" role="img"><line x1="${pad}" y1="${h-pad}" x2="${w-pad}" y2="${h-pad}" class="axis"/><polyline points="${poly}" class="series"/></svg><div class="chart-range"><span>${min.toFixed(3)} ${unit}</span><span>${pts.length} samples</span><span>${max.toFixed(3)} ${unit}</span></div>`;
}
function renderOccupancy(){
  const hours=Number($('#statusWindow').value||0),cut=hours?Date.now()-hours*3600000:0,history=(state.metrics?.statusHistory||[]).filter(x=>!cut||!x.ts||new Date(x.ts).getTime()>=cut),counts={};for(const x of history)counts[x.status]=(counts[x.status]||0)+1;const entries=Object.entries(counts).sort((a,b)=>b[1]-a[1]),total=entries.reduce((n,[,v])=>n+v,0);$('#statusSampleCount').textContent=`${total} events`;
  $('#occupancyChart').innerHTML=entries.length?entries.map(([name,count])=>`<div class="status-row"><div><span>${esc(name)}</span><strong>${count}</strong></div><div class="status-track"><i style="width:${Math.max(2,(count/total)*100)}%"></i></div></div>`).join(''):'<div class="empty">No status history yet.</div>';
}
function renderEvents(){
  const rows=(state.metrics?.recentEvents||[]).slice(0,40).map(e=>`<tr><td>${esc(shortTime(e.ts))}</td><td><strong>${esc(e.action)}</strong></td><td class="traffic-payload"><div class="payload-preview" title="${esc(pretty(e.payload))}">${esc(pretty(e.payload))}</div></td></tr>`);$('#eventLog').innerHTML=table(['Time','Event','Payload'],rows);
}

function filteredMessages(){const dir=$('#trafficDirection').value,type=$('#trafficType').value,action=$('#trafficAction').value.trim().toLowerCase();return state.messages.filter(m=>(!dir||m.direction===dir)&&(!type||m.message_type===type)&&(!action||String(m.action||'').toLowerCase().includes(action)))}
function renderTraffic(){
  const rows=filteredMessages().slice(0,200).map(m=>{const body=m.error||m.payload||{};return `<tr><td>${esc(shortTime(m.ts))}</td><td class="${m.direction.startsWith('Station')?'msg-in':'msg-out'}">${m.direction.startsWith('Station')?'← Station':'→ Station'}</td><td>${esc(m.message_type)}</td><td><strong>${esc(m.action||'—')}</strong></td><td>${esc(m.unique_id||'—')}</td><td class="traffic-payload"><details><summary>View JSON</summary><pre>${esc(pretty(body))}</pre></details></td></tr>`});
  $('#trafficTable').innerHTML=table(['Time','Direction','Type','Action','Unique ID','Payload / error'],rows);const last=state.messages[0];$('#lastAction').textContent=last?.action||last?.message_type||'—';$('#lastActionTime').textContent=last?shortTime(last.ts):'No OCPP traffic yet';
}
function renderTransactions(){
  const active=state.transactions.filter(t=>t.status==='Active');if(active.length&&!$('#transactionId').value)$('#transactionId').value=active[0].ocpp_transaction_id||active[0].id||'';$('#transactionCountLabel').textContent=`${state.transactions.length} stored`;
  const makeRows=items=>items.map(t=>`<tr><td><button class="link-btn tx-pick" data-tx="${esc(t.ocpp_transaction_id||t.id)}">${esc(t.ocpp_transaction_id||t.id)}</button></td><td><span class="status ${t.status==='Active'?'online':'offline'}">${esc(t.status)}</span></td><td>${esc(t.evse_id??'—')} / ${esc(t.connector_id??'—')}</td><td>${esc(t.id_token||'—')}</td><td>${esc(shortTime(t.started_at))}</td><td>${esc(shortTime(t.ended_at))}</td><td>${esc(t.last_meter??t.meter_stop??t.meter_start??'—')}</td></tr>`);
  const headers=['Transaction','State','EVSE / Connector','ID token','Started','Ended','Meter'];$('#transactionsTable').innerHTML=table(headers,makeRows(state.transactions.slice(0,200)));$('#recentTransactionsTable').innerHTML=table(headers,makeRows(state.transactions.slice(0,8)));
}

function currentIds(){return {connectorId:Number($('#connectorId').value||1),evseId:Number($('#evseId').value||1),idToken:$('#idToken').value.trim()||'TEST-TAG-001',transactionId:$('#transactionId').value.trim()}}
function buildCommand(kind){
  const s=state.station,{connectorId,evseId,idToken,transactionId}=currentIds();if(!s)throw new Error('Station not loaded');const v=s.protocol;let action,payload,label,risky=true;
  if(kind==='softReset'){action='Reset';payload=v==='1.6'?{type:'Soft'}:{type:'OnIdle'};label='Soft reset'}
  else if(kind==='hardReset'){action='Reset';payload=v==='1.6'?{type:'Hard'}:{type:'Immediate'};label='Hard reset'}
  else if(kind==='block'){action='ChangeAvailability';payload=v==='1.6'?{connectorId,type:'Inoperative'}:{operationalStatus:'Inoperative',evse:{id:evseId,connectorId}};label='Block charger / connector'}
  else if(kind==='unblock'){action='ChangeAvailability';payload=v==='1.6'?{connectorId,type:'Operative'}:{operationalStatus:'Operative',evse:{id:evseId,connectorId}};label='Unblock charger / connector'}
  else if(kind==='start'){if(v==='1.6'){action='RemoteStartTransaction';payload={idTag:idToken,connectorId}}else{action='RequestStartTransaction';payload={remoteStartId:Math.floor(Date.now()/1000)%2147483647,idToken:{idToken,type:'Central'},evseId}}label='Start transaction'}
  else if(kind==='stop'){if(!transactionId)throw new Error('Enter a Transaction ID first');if(v==='1.6'){action='RemoteStopTransaction';const n=Number(transactionId);if(!Number.isFinite(n))throw new Error('OCPP 1.6 transaction ID must be numeric');payload={transactionId:n}}else{action='RequestStopTransaction';payload={transactionId}}label='Stop transaction'}
  else if(kind==='status'){action='TriggerMessage';payload=v==='1.6'?{requestedMessage:'StatusNotification',connectorId}:{requestedMessage:'StatusNotification',evse:{id:evseId,connectorId}};label='Retrieve status';risky=false}
  else if(kind==='unlock'){action='UnlockConnector';payload=v==='1.6'?{connectorId}:{evseId,connectorId};label='Unlock connector'}
  else throw new Error(`Unknown command ${kind}`);return {action,payload,label,risky};
}
function openConfirm(cmd){state.pendingCommand=cmd;$('#modalTitle').textContent=cmd.label;$('#modalText').textContent=cmd.risky?'This command can change charger state. Verify and edit the payload before sending.':'Verify the OCPP request before sending.';$('#modalAction').value=cmd.action;$('#modalPayload').value=pretty(cmd.payload);$('#confirmModal').classList.remove('hidden')}
async function openTemplate(action,label=action,risky=true){try{if(!state.actions.has(action))throw new Error(`${action} is not available in OCPP ${state.station.protocol}`);const payload=await api(`/api/template/${encodeURIComponent(state.station.protocol)}/${encodeURIComponent(action)}`);openConfirm({action,payload,label,risky})}catch(e){toast(e.message)}}
async function openSpecial(name){const v=state.station.protocol;if(name==='diagnostics')return openTemplate(v==='1.6'?'GetDiagnostics':'GetLog','Get diagnostics / log',false);if(name==='clearLocalList'){const action='SendLocalList';const payload=v==='1.6'?{listVersion:0,localAuthorizationList:[],updateType:'Full'}:{versionNumber:0,localAuthorizationList:[],updateType:'Full'};return openConfirm({action,payload,label:'Clear local list',risky:true})}}
async function sendPendingCommand(){const cmd=state.pendingCommand;if(!cmd)return;let payload;try{payload=JSON.parse($('#modalPayload').value||'{}')}catch(e){return toast(`Invalid JSON: ${e.message}`)}$('#modalSend').disabled=true;$('#modalSend').textContent='Sending…';try{const r=await api(`/api/stations/${encodeURIComponent(identity)}/command`,{method:'POST',body:JSON.stringify({action:cmd.action,payload})});$('#commandResult').className='command-result ok';$('#commandResult').textContent=pretty(r);toast(`${cmd.label}: response received`);closeModal();await refreshData()}catch(e){$('#commandResult').className='command-result error';$('#commandResult').textContent=e.message;toast(e.message)}finally{$('#modalSend').disabled=false;$('#modalSend').textContent='Send command'}}
function closeModal(){$('#confirmModal').classList.add('hidden');state.pendingCommand=null}

async function clearCommunication(){if(!confirm('Clear this charger communication history from the local CSMS database?'))return;try{const r=await api(`/api/stations/${encodeURIComponent(identity)}/communication/clear`,{method:'POST',body:'{}'});toast(`Cleared ${r.cleared} messages`);await loadMessages()}catch(e){toast(e.message)}}
async function disconnectAndClear(){if(!confirm('End locally active transactions, clear communication history, and disconnect this charger?'))return;try{const r=await api(`/api/stations/${encodeURIComponent(identity)}/maintenance`,{method:'POST',body:JSON.stringify({endActiveTransactions:true,clearCommunicationLog:true,disconnect:true})});$('#commandResult').textContent=pretty(r);toast('Maintenance action completed');await refreshData()}catch(e){toast(e.message)}}

async function loadConfiguration(){if(!isConnected())return toast('Station is offline');try{if(state.station.protocol==='1.6'){const r=await api(`/api/stations/${encodeURIComponent(identity)}/configuration/read`,{method:'POST',body:'{}'});renderConfig16(r.payload?.configurationKey||[],r.payload?.unknownKey||[])}else await getVariable();toast('Configuration response received')}catch(e){toast(e.message)}}
function renderConfig16(items,unknown=[]){state.config16Items=items;const q=($('#configFilter')?.value||'').toLowerCase();const rows=items.filter(i=>!q||String(i.key).toLowerCase().includes(q)).map(i=>`<tr><td><button class="link-btn config-pick" data-key="${esc(i.key)}" data-value="${esc(i.value)}">${esc(i.key)}</button></td><td>${esc(i.value)}</td><td>${i.readonly?'Read only':'Writable'}</td></tr>`);$('#config16Table').innerHTML=table(['Key','Value','Access'],rows)+(unknown.length?`<p class="muted">Unknown: ${unknown.map(esc).join(', ')}</p>`:'')}
async function setConfig16(){const key=$('#config16Key').value.trim(),value=$('#config16Value').value;if(!key)return toast('Enter a key');try{const r=await api(`/api/stations/${encodeURIComponent(identity)}/configuration/write`,{method:'POST',body:JSON.stringify({key,value})});$('#commandResult').className='command-result ok';$('#commandResult').textContent=pretty(r);toast(`${key} change sent`);await loadConfiguration()}catch(e){toast(e.message)}}
function variableItem(){return {component:{name:$('#componentName').value.trim()},variable:{name:$('#variableName').value.trim()},attributeType:'Actual'}}
async function getVariable(){try{const r=await api(`/api/stations/${encodeURIComponent(identity)}/configuration/read`,{method:'POST',body:JSON.stringify({items:[variableItem()]})});$('#variableResult').textContent=pretty(r);return r}catch(e){$('#variableResult').textContent=e.message;throw e}}
async function setVariable(){try{const i=variableItem();const r=await api(`/api/stations/${encodeURIComponent(identity)}/configuration/write`,{method:'POST',body:JSON.stringify({items:[{...i,attributeValue:$('#variableValue').value}]})});$('#variableResult').textContent=pretty(r);toast('SetVariables response received')}catch(e){$('#variableResult').textContent=e.message;toast(e.message)}}

function renderActionAvailability(){
  $$('[data-action]').forEach(b=>{const a=b.dataset.action;b.disabled=!isConnected()||!state.actions.has(a);b.title=state.actions.has(a)?'':`${a} not supported by OCPP ${state.station?.protocol||''}`});
  $$('[data-command],[data-special]').forEach(b=>b.disabled=!isConnected());
  if(state.station?.protocol==='1.6'){$$('.template-command[data-action="GetBaseReport"],.template-command[data-action="GetReport"],.template-command[data-action="GetCertificateStatus"]').forEach(x=>x.classList.add('hidden'))}else{$$('.template-command[data-action="GetBaseReport"],.template-command[data-action="GetReport"]').forEach(x=>x.classList.remove('hidden'))}
}

function renderTestCases(){
  const groups={};for(const t of state.testCases){(groups[t.category]??=[]).push(t)}
  $('#testCases').innerHTML=Object.entries(groups).map(([cat,items])=>`<details class="test-group"><summary>${esc(cat)} <span>${items.length}</span></summary><div class="test-case-list">${items.map(t=>`<div class="test-case"><div><strong>${esc(t.action)}</strong><small>${esc(t.direction)} · ${t.risky?'state-changing':'safe'}</small></div><span class="test-risk ${t.risky?'risky':'safe'}">${t.risky?'RISKY':'SAFE'}</span><button class="secondary run-one-test" data-test="${esc(t.action)}" data-risky="${t.risky}">Run</button></div>`).join('')}</div></details>`).join('')
}
function renderTestResult(run){
  $('#testSummary').className=`test-summary ${run.failed?'error':'ok'}`;$('#testSummary').textContent=`Run ${run.runId??'schema'}: ${run.passed} PASS · ${run.failed} FAIL · ${run.skipped||0} SKIPPED`;
  const rows=(run.results||[]).map(r=>`<tr><td>${esc(r.action)}</td><td>${esc(r.direction||r.protocol||'—')}</td><td><span class="test-status ${String(r.status).toLowerCase()}">${esc(r.status)}</span></td><td>${esc(r.durationMs??'—')}</td><td>${esc(r.detail||'')}</td></tr>`);$('#testResults').innerHTML=table(['Action','Direction / Protocol','Result','ms','Detail'],rows)
}
async function runTests(actions=null,includeRisky=false){if(!isConnected())return toast('Station is offline');try{$('#testSummary').textContent='Running tests…';const r=await api(`/api/stations/${encodeURIComponent(identity)}/tests/run`,{method:'POST',body:JSON.stringify({actions,includeRisky,timeout:8})});renderTestResult(r);toast(`Tests finished: ${r.passed} pass, ${r.failed} fail`)}catch(e){toast(e.message);$('#testSummary').textContent=e.message}}
async function runSchemaTests(){try{$('#testSummary').textContent='Running schema tests…';const r=await api('/api/self-tests/schemas',{method:'POST',body:'{}'});renderTestResult(r);toast(`Schema tests: ${r.passed}/${r.total}`)}catch(e){toast(e.message)}}

function renderAuthorizeRule(){
  const rule=state.responseRules.find(r=>r.protocol===state.station?.protocol&&r.action==='Authorize');let status='Default';
  if(rule?.payload?.idTagInfo?.status)status=rule.payload.idTagInfo.status;if(rule?.payload?.idTokenInfo?.status)status=rule.payload.idTokenInfo.status;
  $('#authorizeStatus').value=[...$('#authorizeStatus').options].some(o=>o.value===status)?status:'Default';$('#authorizeRuleState').textContent=status;
}
async function saveAuthorizeRule(){
  if(!state.station)return;const status=$('#authorizeStatus').value,v=state.station.protocol;
  try{if(status==='Default'){await api(`/api/response-rules/${encodeURIComponent(v)}/Authorize`,{method:'DELETE'});toast('Authorize behavior reset to default')}
  else{const payload=v==='1.6'?{idTagInfo:{status}}:{idTokenInfo:{status}};await api(`/api/response-rules/${encodeURIComponent(v)}/Authorize`,{method:'PUT',body:JSON.stringify(payload)});toast(`Authorize will return ${status}`)}await loadResponseRules()}catch(e){toast(e.message)}
}
async function simulateAuthorize(){
  const token=$('#authorizeToken').value.trim()||'TEST-TAG-001',v=state.station?.protocol;if(!v)return;
  const payload=v==='1.6'?{idTag:token}:{idToken:{idToken:token,type:'Central'}};
  try{const r=await api(`/api/virtual-chargers/${encodeURIComponent(identity)}/call`,{method:'POST',body:JSON.stringify({action:'Authorize',payload,timeout:5})});$('#authorizeResult').textContent=pretty(r);toast('Authorize simulated through virtual charger')}catch(e){$('#authorizeResult').textContent=e.message;toast('Simulation works only when this station is the built-in virtual charger')}
}

function renderSettings(){const s=state.settings||{};$('#friendlyName').value=s.friendly_name||'';$('#stationReference').value=s.reference||'';$('#physicalId').value=s.physical_id||'';$('#securityProfile').value=s.security_profile||'None';$('#allowedIp').value=s.allowed_ip||'';$('#locationName').value=s.location_name||'';$('#latitude').value=s.latitude??'';$('#longitude').value=s.longitude??'';$('#remoteAccessUrl').value=s.remote_access_url||'';$('#remoteAccessEnabled').checked=!!s.remote_access_enabled;$('#debugLogging').checked=!!s.debug_logging;$('#stationNotes').value=s.notes||''}
async function saveSettings(){const body={friendly_name:$('#friendlyName').value,reference:$('#stationReference').value,physical_id:$('#physicalId').value,security_profile:$('#securityProfile').value,allowed_ip:$('#allowedIp').value,location_name:$('#locationName').value,latitude:$('#latitude').value||null,longitude:$('#longitude').value||null,remote_access_url:$('#remoteAccessUrl').value,remote_access_enabled:$('#remoteAccessEnabled').checked,debug_logging:$('#debugLogging').checked,notes:$('#stationNotes').value};try{state.settings=await api(`/api/stations/${encodeURIComponent(identity)}/settings`,{method:'PUT',body:JSON.stringify(body)});renderSettings();toast('Station data saved')}catch(e){toast(e.message)}}

async function disconnect(){if(!isConnected())return;try{await api(`/api/stations/${encodeURIComponent(identity)}/disconnect`,{method:'POST',body:'{}'});toast('Station disconnected');await refreshData()}catch(e){toast(e.message)}}
async function refreshData(){if(state.refreshing)return;state.refreshing=true;try{$('#healthDot').classList.add('ok');$('#healthText').textContent='CSMS running';await loadStation();await Promise.all([loadMessages(),loadTransactions(),loadMetrics(),loadSettings(),loadActions(),loadTestCases(),loadResponseRules()])}catch(e){$('#healthDot').classList.remove('ok');$('#healthText').textContent='Load failed';toast(e.message)}finally{state.refreshing=false}}

$$('.command-card[data-command]').forEach(b=>b.onclick=()=>{try{openConfirm(buildCommand(b.dataset.command))}catch(e){toast(e.message)}});
$$('[data-action]').forEach(b=>b.onclick=()=>openTemplate(b.dataset.action,b.dataset.label||b.textContent.trim(),true));
$$('[data-special]').forEach(b=>b.onclick=()=>openSpecial(b.dataset.special));
$('#modalCancel').onclick=closeModal;$('#modalSend').onclick=sendPendingCommand;$('#confirmModal').onclick=e=>{if(e.target.id==='confirmModal')closeModal()};
$('#clearCommLog').onclick=clearCommunication;$('#disconnectClear').onclick=disconnectAndClear;
$('#loadConfiguration').onclick=loadConfiguration;$('#setConfig16').onclick=setConfig16;$('#getVariable').onclick=getVariable;$('#setVariable').onclick=setVariable;$('#configFilter').oninput=()=>renderConfig16(state.config16Items||[]);$('#config16Table').onclick=e=>{const b=e.target.closest('.config-pick');if(!b)return;$('#config16Key').value=b.dataset.key;$('#config16Value').value=b.dataset.value};
$('#trafficDirection').onchange=renderTraffic;$('#trafficType').onchange=renderTraffic;$('#trafficAction').oninput=renderTraffic;$('#applyTrafficFilter').onclick=renderTraffic;$('#chartMetric').onchange=renderMeterChart;$('#chartWindow').onchange=renderMeterChart;$('#statusWindow').onchange=renderOccupancy;
$('#transactionsTable').onclick=$('#recentTransactionsTable').onclick=e=>{const b=e.target.closest('.tx-pick');if(b){$('#transactionId').value=b.dataset.tx;toast(`Selected transaction ${b.dataset.tx}`)}};
$('#runSafeTests').onclick=()=>runTests(null,false);$('#runFullTests').onclick=()=>{if(confirm('Run the full suite including state-changing commands? This can reset, stop, reconfigure, or update the charger.'))runTests(null,true)};$('#runSchemaTests').onclick=runSchemaTests;
$('#testCases').onclick=e=>{const b=e.target.closest('.run-one-test');if(!b)return;const risky=b.dataset.risky==='true';if(risky&&!confirm(`Run state-changing test ${b.dataset.test}?`))return;runTests([b.dataset.test],risky)};
$('#saveAuthorizeRule').onclick=saveAuthorizeRule;$('#simulateAuthorize').onclick=simulateAuthorize;$('#saveStationData').onclick=saveSettings;$('#disconnectBtn').onclick=disconnect;$('#refreshNow').onclick=refreshData;
function activateTab(tabId,{updateHash=true}={}){
  const target=document.getElementById(tabId);
  if(!target||!target.classList.contains('tab-section'))return;
  $$('.tab-section').forEach(s=>s.classList.toggle('active',s.id===tabId));
  $$('.station-tab').forEach(b=>b.classList.toggle('active',b.dataset.tab===tabId));
  $$('.detail-nav[href^="#"]').forEach(a=>a.classList.toggle('active',a.getAttribute('href')===`#${tabId}`));
  if(updateHash)history.replaceState(null,'',`#${tabId}`);
  window.scrollTo({top:0,behavior:'smooth'});
}
$$('.station-tab').forEach(b=>b.onclick=()=>activateTab(b.dataset.tab));
$$('.detail-nav[href^="#"]').forEach(a=>a.onclick=e=>{e.preventDefault();activateTab(a.getAttribute('href').slice(1))});
$$('.tab-link').forEach(a=>a.onclick=e=>{e.preventDefault();activateTab(a.dataset.tab||a.getAttribute('href').slice(1))});
$('#getParametersShortcut').onclick=async()=>{activateTab('configuration');await loadConfiguration()};
window.addEventListener('hashchange',()=>activateTab(location.hash.slice(1)||'dashboard',{updateHash:false}));
activateTab(location.hash.slice(1)||'dashboard',{updateHash:false});

refreshData();setInterval(refreshData,4000);
