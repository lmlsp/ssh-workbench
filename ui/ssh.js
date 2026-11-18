'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const state = { view:'terminal', model:{ connections:[], apps:[], commands:[], flows:[] }, terminals:[],
  session:null, packet:null, epoch:0, revision:0, panes:[], editor:null, ready:false, logsBusy:false };
const activePhases = ['connecting', 'switching', 'dispatch', 'running', 'between', 'paused'];
let terminal, fit;

async function api(path, data) {
  const options = data === undefined ? {} : { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data) };
  const response = await fetch('/api/' + path, options);
  const body = await response.json();
  if (!response.ok) throw Object.assign(new Error(body.error), { status:response.status });
  return body;
}

function message(selector, text = '') { const node = $(selector); node.textContent = text; node.hidden = !text; }
function byId(collection, id) { return state.model[collection].find(row => row.id === id); }
function button(label, click) { const node = document.createElement('button'); node.type='button'; node.textContent=label; node.onclick=click; return node; }
function select(node, options, current = node.value) {
  node.replaceChildren(...options.map(([id, label]) => new Option(label, id)));
  if (options.some(([id]) => id === current)) node.value = current;
}
function connectionOptions() { return state.model.connections.map(row => [row.id, row.name]); }
function heading() { $('#head-status').textContent = `${state.model.connections.length} 条 SSH 连接`; }

function row(title, detail, actions) {
  const li = document.createElement('li'); const words = document.createElement('div');
  const label = document.createElement('strong'); const subtitle = document.createElement('p');
  label.textContent = title; subtitle.textContent = detail; words.append(label, subtitle);
  const buttons = document.createElement('div'); buttons.className = 'actions'; buttons.append(...actions);
  li.append(words, buttons); return li;
}

function renderLists() {
  heading();
  select($('#terminal-connection'), connectionOptions());
  const hasConnections = state.model.connections.length > 0;
  $('#terminal-empty').hidden = hasConnections;
  $('#servers-empty').hidden = hasConnections;
  $('#logs-empty').hidden = state.model.apps.length > 0;
  $('#attach-form button[type="submit"]').disabled = !hasConnections;
  $('#new-command').disabled = !hasConnections;
  $('#new-flow').disabled = state.model.commands.length === 0;
  $('#add-app').disabled = !hasConnections;
  $('#add-log').disabled = !state.model.apps.length || state.panes.length >= 4;
  $('#server-list').replaceChildren(...state.model.connections.map(item => row(item.name, `${item.user}@${item.host}:${item.port}`, [
    button('编辑', () => openEditor('connections', item)), button('连接', () => connect(item.id)),
  ])));
  $('#app-list').replaceChildren(...state.model.apps.map(item => row(item.name, `${byId('connections', item.connection)?.name || '连接'} · ${item.sources.length} 个日志来源`, [
    button('编辑', () => openEditor('apps', item)), button('看日志', () => { addPane(item.id, true); show('logs'); }),
  ])));
  for (const [collection, target] of [['flows','#flow-list'], ['commands','#command-list']]) {
    $(target).replaceChildren(...state.model[collection].map(item => row(item.name,
      [byId('connections',item.connection)?.name, byId('apps',item.app)?.name, item.type, collection==='commands' ? item.user : `${item.steps.length} 步`].filter(Boolean).join(' · '),
      [button('编辑', () => openEditor(collection, item)), button('执行', () => askRun(collection, item, 'terminal'))])));
  }
  for (const pane of state.panes) {
    const app = byId('apps', pane.app.id);
    if (!app) continue;
    select(pane.appSelect, appOptions(), app.id);
    if (JSON.stringify(app.sources) !== JSON.stringify(pane.app.sources)) chooseApp(pane, app.id);
    else { pane.app = app; quickList(pane); }
  }
}

async function load() { state.model = await api('state'); renderLists(); }

function show(view) {
  state.view = view;
  sessionStorage.setItem('workbenchView', view);
  $$('.view').forEach(node => { node.hidden = node.id !== 'view-' + view; });
  $$('.navigation button').forEach(node => node.dataset.view === view ? node.setAttribute('aria-current','page') : node.removeAttribute('aria-current'));
  placeTerminal();
  if (view === 'logs') void loadLogs();
}

function placeTerminal() {
  const inLogs = state.view === 'logs' && state.session?.inLogs;
  const active = state.session && (state.view === 'terminal' || inLogs);
  const region = $('#terminal-region');
  $('#logs-terminal').hidden = !inLogs;
  $('#logs-layout').classList.toggle('with-terminal', Boolean(inLogs));
  region.hidden = !active;
  if (active) {
    $(inLogs ? '#logs-terminal' : '#terminal-main').append(region);
    initializeTerminal();
    requestAnimationFrame(resizeTerminal);
  }
}

function initializeTerminal() {
  if (terminal) return;
  terminal = new Terminal({ fontFamily:'Cascadia Mono, Noto Sans Mono CJK SC, monospace', fontSize:14, cursorBlink:true,
    theme:{ background:'#132136', foreground:'#e4edf9', cursor:'#abceff', selectionBackground:'#345989' }, scrollback:4500 });
  fit = new FitAddon.FitAddon(); terminal.loadAddon(fit); terminal.open($('#xterm'));
  terminal.parser.registerOscHandler(52, () => true);
  terminal.onData(value => queueInput(value));
  terminal.onResize(({cols,rows}) => { if (state.session && !state.packet?.closed) void api(`terminal/${state.session.id}/size`,{cols,rows}).catch(() => {}); });
  new ResizeObserver(resizeTerminal).observe($('#xterm'));
}

let pendingInput = '', inputTimer = null, inputFor = null, inputChain = Promise.resolve(), takeoverGate = null;
function queueInput(value) {
  if (!state.session || state.packet?.closed) return;
  if (inputFor && inputFor !== state.session.id) flushInput();
  inputFor = state.session.id; pendingInput += value;
  if (!inputTimer) inputTimer = setTimeout(flushInput, 10);
}
function flushInput() {
  clearTimeout(inputTimer); inputTimer = null;
  const id = inputFor, content = pendingInput;
  pendingInput = ''; inputFor = null;
  if (!id || !content) return;
  const chunks = Array.from(content);
  const gate = takeoverGate?.id === id ? takeoverGate.promise : null;
  inputChain = inputChain.then(async () => {
    if (gate) await gate;
    for (let at=0; at<chunks.length; at+=4000) await api(`terminal/${id}/input`,{text:chunks.slice(at,at+4000).join('')});
    if (state.session?.id === id) message('#terminal-error');
  }).catch(error => { if (state.session?.id === id) message('#terminal-error',error.message); });
}
function resizeTerminal() { if (fit && !$('#terminal-region').hidden && $('#xterm').offsetWidth > 0) fit.fit(); }

let oldHistory = '';
function renderPacket(packet) {
  state.packet = packet;
  const name = byId('connections',packet.connection)?.name || 'SSH';
  $('#term-name').textContent = packet.run?.name || name;
  $('#term-context').textContent = packet.closed ? '连接已断开' : packet.context ? `${packet.context.user} · ${packet.context.directory}` : '正在连接。SSH 提示可直接在终端处理。';
  $('#close-terminal').hidden = packet.closed;
  $('#run-info').hidden = !packet.run;
  const run = packet.run;
  if (run) {
    $('#run-message').textContent = run.message;
    const phase = run.phase;
    const statuses = {pending:'待执行',running:'执行中',succeeded:'通过',failed:'失败',manual:'手工完成','needs-review':'待核对'};
    $('#run-steps').replaceChildren(...run.steps.map(step => { const li = document.createElement('li'); li.dataset.status=step.status; li.textContent=`${step.name} · ${statuses[step.status]}`; return li; }));
    $('[data-action="takeover"]').hidden = !activePhases.includes(phase) || run.takeover || phase === 'paused';
    for (const action of ['retry','next']) {
      const target = $(`[data-action="${action}"]`);
      target.hidden = phase !== 'paused'; target.disabled = !packet.ready || packet.context?.user !== run.user;
    }
    $('[data-action="end"]').hidden = !activePhases.includes(phase);
  }
  const fingerprint = packet.history.map(entry => entry.id).join(',');
  if (fingerprint !== oldHistory) {
    oldHistory = fingerprint;
    $('#history-empty').hidden = packet.history.length > 0;
    $('#history-list').replaceChildren(...packet.history.map(entry => {
      const li=document.createElement('li'), text=document.createElement('code');
      text.textContent=entry.script;
      li.append(text, button('保存为快捷指令', () => openEditor('commands',{connection:packet.connection,
        app: logApplicationFor(packet.connection,entry.directory), ...entry, id:undefined },true)));
      return li;
    }));
  }
}

function logApplicationFor(connection, directory) {
  const candidates = state.model.apps.filter(item => item.connection === connection && item.sources.some(source => source.path.startsWith(directory.replace(/\/$/,'') + '/')));
  return candidates.length === 1 ? candidates[0].id : '';
}

async function readTerminal(epoch) {
  while (state.epoch === epoch && state.session && state.ready) {
    const id=state.session.id;
    try {
      const packet=await api(`terminal/${id}/events?after=${state.revision}&wait=1`);
      if (state.epoch !== epoch) return;
      if (packet.reset) terminal.reset();
      if (packet.text) terminal.write(Uint8Array.from(atob(packet.text),char=>char.charCodeAt(0)));
      state.revision = packet.revision;
      renderPacket(packet);
      if (packet.closed) { void terminals(); return; }
    } catch (error) {
      if (state.epoch !== epoch) return;
      if (error.status === 401) return showLogin();
      if (error.status === 400) { sessionStorage.removeItem('workbenchTerminal'); return message('#terminal-error',error.message); }
      message('#terminal-error','终端连接暂不可用，正在重连页面。');
      await new Promise(resolve=>setTimeout(resolve,1000));
    }
  }
}

async function terminals() {
  state.terminals=await api('terminals');
  select($('#switch-session'), [['','选择终端'],...state.terminals.map(t=>[t.id,t.name+(t.closed?'（已断开）':'')])], state.session?.id || '');
}

function attach(id, inLogs = false) {
  if (state.session?.id === id) {
    state.session.inLogs=inLogs;
    sessionStorage.setItem('workbenchTerminalPlacement',inLogs ? 'logs' : 'terminal');
    placeTerminal();
    return;
  }
  flushInput();
  state.session={id,inLogs}; state.packet=null; state.revision=0; oldHistory='';
  state.epoch+=1;
  sessionStorage.setItem('workbenchTerminal',id);
  sessionStorage.setItem('workbenchTerminalPlacement',inLogs ? 'logs' : 'terminal');
  $('#history-list').replaceChildren(); $('#history-empty').hidden=false;
  $('#run-info').hidden=true; $('#term-name').textContent='SSH'; $('#term-context').textContent='正在连接…';
  message('#terminal-error');
  placeTerminal();
  initializeTerminal();
  terminal.reset(); terminal.focus();
  void readTerminal(state.epoch); void terminals();
}

async function connect(id) {
  message('#notice');
  try { const job=await api('terminal',{connection:id}); show('terminal'); attach(job.id); }
  catch(error) { message('#notice',error.message); }
}

async function run(collection,item,values,where) {
  const launched=await api('run',{kind:collection==='flows'?'flow':'command',id:item.id,params:values,terminal:state.session?.id});
  show(where);
  attach(launched.id,where==='logs');
}

function askRun(collection,item,where) {
  const steps=collection==='flows'?item.steps.map(id=>byId('commands',id)).filter(Boolean):[item];
  const fields=Object.assign({},...steps.map(step=>step.params));
  message('#notice');
  if (!Object.keys(fields).length) { void run(collection,item,{},where).catch(error=>message('#notice',error.message)); return; }
  state.parameters={collection,item,where};
  $('#parameters-title').textContent=item.name;
  $('#parameters-inputs').replaceChildren(...Object.entries(fields).map(([key,label])=>{
    const wrap=document.createElement('label'), input=document.createElement('input');
    wrap.textContent=label; input.name=key; input.required=true; input.maxLength=4000; wrap.append(input); return wrap;
  }));
  message('#parameters-error'); $('#parameters').showModal();
}

function appOptions() { return state.model.apps.map(app=>[app.id,`${app.name} · ${byId('connections',app.connection)?.name || ''}`]); }

function chooseApp(pane,id) {
  pane.app=byId('apps',id);
  select(pane.appSelect,appOptions(),id);
  select(pane.sourceSelect,pane.app.sources.map(source=>[source.id,source.name]));
  resetLog(pane); quickList(pane);
}

function resetLog(pane) {
  pane.source=pane.sourceSelect.value; pane.cursor=null; pane.lines=[];
  $('.log-text',pane.node).textContent=''; $('.pane-status',pane.node).textContent='正在读取';
  void loadLogs();
}

function quickList(pane) {
  const actions=['flows','commands'].flatMap(collection=>state.model[collection].filter(row=>row.connection===pane.app.connection && row.app===pane.app.id).map(item=>({collection,item})));
  const filter=$('.quick-filter',pane.node);
  select(filter,[['','全部类型'],...Array.from(new Set(actions.map(a=>a.item.type).filter(Boolean))).map(type=>[type,type])]);
  const filtered=actions.filter(({item})=>!filter.value || item.type===filter.value);
  $('.quick-list',pane.node).replaceChildren(...filtered.map(({collection,item})=>row(item.name,item.type||'',[
    button('执行',()=>askRun(collection,item,'logs'))])));
  $('.quick-empty',pane.node).hidden=filtered.length>0;
}

function addPane(id, replace=false) {
  if (state.panes.length>=4 && replace) { chooseApp(state.panes[0],id); return; }
  if (state.panes.length>=4 || !state.model.apps.length) return;
  const fragment=$('#log-template').content.cloneNode(true), node=$('.log-pane',fragment);
  const pane={node, appSelect:$('.log-app',node), sourceSelect:$('.log-source',node), paused:false};
  state.panes.push(pane); $('#logs-grid').append(fragment);
  chooseApp(pane,id || state.model.apps[state.panes.length%state.model.apps.length].id);
  pane.appSelect.onchange=()=>chooseApp(pane,pane.appSelect.value);
  pane.sourceSelect.onchange=()=>resetLog(pane);
  $('.pause',node).onclick=()=>{ pane.paused=!pane.paused; $('.pause',node).textContent=pane.paused?'继续':'暂停'; $('.pane-status',node).textContent=pane.paused?'已暂停':'正在读取'; void loadLogs(); };
  $('.remove',node).onclick=()=>{ state.panes=state.panes.filter(row=>row!==pane); node.remove(); $('#add-log').disabled=false; };
  $('.quick',node).onclick=()=>{ const menu=$('.quick-menu',node); menu.hidden=!menu.hidden; $('.quick',node).setAttribute('aria-expanded',String(!menu.hidden)); quickList(pane); };
  $('.quick-filter',node).onchange=()=>quickList(pane);
  $('#add-log').disabled=state.panes.length>=4;
}

async function loadLogs() {
  if (!state.ready || state.logsBusy || state.view!=='logs') return;
  const active=state.panes.filter(pane=>!pane.paused && pane.source);
  if (!active.length) return;
  const query=active.map(pane=>({source:pane.source,cursor:pane.cursor}));
  state.logsBusy=true;
  try {
    const rows=await api('logs',{sources:query});
    rows.forEach((row,index)=>{
      const pane=active[index];
      if (pane.paused || pane.source!==query[index].source || pane.cursor!==query[index].cursor) return;
      const status=$('.pane-status',pane.node);
      if (row.error) return void(status.textContent=row.error);
      pane.cursor=row.cursor;
      const log=$('.log-text',pane.node);
      if (row.lines.length) {
        const atBottom=pane.lines.length===0 || log.scrollHeight-log.clientHeight-log.scrollTop<25;
        pane.lines=pane.lines.concat(row.lines).slice(-520); log.textContent=pane.lines.join('\n');
        if (atBottom) log.scrollTop=log.scrollHeight;
      }
      status.textContent=pane.lines.length?'实时跟踪':'已连接，等待新日志';
    });
  } catch(error) {
    if (error.status===401) return showLogin();
    active.forEach(pane=>{$('.pane-status',pane.node).textContent=error.message;});
  } finally { state.logsBusy=false; }
}

function sourceRow(source={}) {
  const line=document.createElement('div'); line.className='source-row'; line.dataset.id=source.id||'';
  const name=document.createElement('input'), path=document.createElement('input');
  name.placeholder='名称，例如运行日志'; name.setAttribute('aria-label','日志名称'); name.value=source.name||'';
  path.placeholder='/path/to/service.log'; path.setAttribute('aria-label','日志文件路径'); path.value=source.path||'';
  line.append(name,path,button('移除',()=>line.remove())); $('#edit-sources').append(line);
}

function showContext(app='') {
  const connection=$('#edit-connection').value;
  select($('#edit-app'),[['','不关联应用'],...state.model.apps.filter(item=>item.connection===connection).map(item=>[item.id,item.name])],app);
  select($('#step-choice'),state.model.commands.filter(item=>item.connection===connection).map(item=>[item.id,`${item.name} · ${item.user}`]));
}

function drawSteps() {
  $('#step-list').replaceChildren(...state.editor.steps.map((id,index)=>{
    const step=byId('commands',id), li=document.createElement('li');
    li.textContent=step?.name || '未知指令';
    const move=delta=>{const rows=state.editor.steps; [rows[index],rows[index+delta]]=[rows[index+delta],rows[index]]; drawSteps();};
    const up=button('上移',()=>move(-1)), down=button('下移',()=>move(1)); up.disabled=index===0; down.disabled=index===state.editor.steps.length-1;
    li.append(up,down,button('移除',()=>{state.editor.steps.splice(index,1);drawSteps();})); return li;
  }));
}

function drawLabels() {
  const matches=[...new Set(Array.from($('#edit-script').value.matchAll(/\{\{([a-zA-Z][a-zA-Z0-9_]{0,31})\}\}/g),m=>m[1]))];
  $('#parameter-labels').replaceChildren(...matches.map(key=>{
    const label=document.createElement('label'), input=document.createElement('input');
    label.textContent=`${key} 的提示`; input.value=state.editor.params[key]||key; input.maxLength=40;
    input.oninput=()=>{state.editor.params[key]=input.value;}; label.append(input);return label;
  }));
}

function openEditor(collection,item={},fresh=false) {
  if ($('#editor').open) return;
  state.editor={collection,id:item.id,steps:[...(item.steps||[])],params:{...item.params},passwordSet:!!item.passwordSet,secretTouched:false};
  const server=collection==='connections', app=collection==='apps', command=collection==='commands', flow=collection==='flows';
  $('#editor-title').textContent=(item.id?'编辑':'新建')+({connections:'SSH 连接',apps:'应用日志',commands:'快捷指令',flows:'流程'})[collection];
  $('#server-fields').hidden=!server; $('#context-fields').hidden=server; $('#app-field').hidden=app; $('#type-field').hidden=app;
  $('#app-fields').hidden=!app; $('#command-fields').hidden=!command; $('#flow-fields').hidden=!flow;
  $('#parameter-fields').hidden=true; $('#edit-sources').replaceChildren();
  select($('#edit-connection'),connectionOptions(),item.connection||state.packet?.connection||$('#terminal-connection').value);
  $('#edit-name').value=fresh?'':item.name||''; $('#edit-host').value=item.host||''; $('#edit-port').value=item.port||22; $('#edit-login-user').value=item.user||'';
  $('#edit-type').value=item.type||''; $('#edit-user').value=item.user||byId('connections',$('#edit-connection').value)?.user||'';
  $('#edit-directory').value=item.directory||''; $('#edit-script').value=item.script||'';
  $('#edit-secret').value=''; $('#edit-secret').type='password'; $('#edit-secret').placeholder=item.passwordSet?'已保存，输入可更换':'需要 su 时可填写';
  $('#toggle-secret').textContent='显示'; $('#clear-secret').hidden=!item.passwordSet;
  showContext(item.app); drawSteps(); drawLabels();
  if (app) (item.sources||[{}]).forEach(sourceRow);
  message('#editor-error'); $('#editor').showModal();
}

function readEditor() {
  const editor=state.editor;
  const value={id:editor.id,name:$('#edit-name').value};
  if (editor.collection==='connections') return {...value,host:$('#edit-host').value,port:Number($('#edit-port').value),user:$('#edit-login-user').value};
  value.connection=$('#edit-connection').value;
  if (editor.collection==='apps') return {...value,sources:$$('.source-row').map(line=>{const [name,path]=$$('input',line); return {id:line.dataset.id,name:name.value,path:path.value};})};
  value.app=$('#edit-app').value; value.type=$('#edit-type').value;
  if (editor.collection==='flows') return {...value,steps:editor.steps};
  if (editor.secretTouched) value.password=$('#edit-secret').value;
  return {...value,script:$('#edit-script').value,user:$('#edit-user').value,directory:$('#edit-directory').value,params:editor.params};
}

$$('[data-view]').forEach(node=>{node.onclick=()=>show(node.dataset.view);});
$('#attach-form').onsubmit=event=>{event.preventDefault();void connect($('#terminal-connection').value);};
$('#switch-session').onchange=()=>{if($('#switch-session').value) attach($('#switch-session').value);};
$('#close-terminal').onclick=()=>{ if (state.session) void api(`terminal/${state.session.id}/close`,{}).catch(error=>message('#terminal-error',error.message)); };
$$('[data-action]').forEach(node=>{node.onclick=()=>{
  if(!state.session) return;
  const id=state.session.id, action=node.dataset.action, promise=api(`terminal/${id}/control`,{action});
  if(action==='takeover') takeoverGate={id,promise};
  void promise.then(()=>terminal.focus()).catch(error=>message('#terminal-error',error.message))
    .finally(()=>{if(takeoverGate?.promise===promise)takeoverGate=null;});
};});
$('#add-log').onclick=()=>addPane();
$('#new-command').onclick=()=>openEditor('commands'); $('#new-flow').onclick=()=>openEditor('flows');
$('#add-server').onclick=()=>openEditor('connections'); $('#add-app').onclick=()=>openEditor('apps');
$('#add-source').onclick=()=>sourceRow(); $('#edit-connection').onchange=()=>{showContext(); if(state.editor.collection==='flows'){state.editor.steps=[];drawSteps();} if(state.editor.collection==='commands') $('#edit-user').value=byId('connections',$('#edit-connection').value)?.user||'';};
$('#add-step').onclick=()=>{ const choice=$('#step-choice').value; if(choice && state.editor.steps.length < 24){state.editor.steps.push(choice);drawSteps();} };
$('#cancel-edit').onclick=$('#editor-close').onclick=()=>$('#editor').close();
$('#editor').addEventListener('close',()=>{$('#edit-secret').value='';state.editor=null;});
$('#edit-secret').oninput=()=>{state.editor.secretTouched=true;};
$('#clear-secret').onclick=()=>{ $('#edit-secret').value=''; $('#edit-secret').placeholder='保存后清除密码'; state.editor.secretTouched=true; };
$('#toggle-secret').onclick=async()=>{
  const field=$('#edit-secret'), toggle=$('#toggle-secret');
  if(field.type==='text'){field.type='password';toggle.textContent='显示';return;}
  try {
    if(!field.value && state.editor.passwordSet && !state.editor.secretTouched){ const {password}=await api('password',{id:state.editor.id}); field.value=password||''; }
    field.type='text';toggle.textContent='隐藏';
  } catch(error) {message('#editor-error',error.message);}
};
$('#edit-script').oninput=drawLabels;
$('#mark-parameter').onclick=()=>{
  const input=$('#edit-script'); let start=input.selectionStart,end=input.selectionEnd;
  if(start===end) return message('#editor-error','先在命令中选中链接或要替换的值。');
  const quote=input.value[start-1]; if((quote==='"'||quote==="'") && input.value[end]===quote){start-=1;end+=1;}
  state.editor.selection=[start,end]; const url=/https?:|ftp:/.test(input.value.slice(start,end));
  $('#edit-parameter-name').value=url?'buildUrl':`param${Object.keys(state.editor.params).length+1}`;
  $('#edit-parameter-label').value=url?'构建包链接':'';
  message('#editor-error'); $('#parameter-fields').hidden=false; $('#edit-parameter-label').focus();
};
$('#insert-parameter').onclick=()=>{
  const key=$('#edit-parameter-name').value.trim();
  if(!/^[a-zA-Z][a-zA-Z0-9_]{0,31}$/.test(key))return message('#editor-error','参数名请用字母、数字或下划线，并用字母开头。');
  state.editor.params[key]=$('#edit-parameter-label').value.trim()||key;
  const [start,end]=state.editor.selection;
  $('#edit-script').setRangeText(`{{${key}}}`,start,end,'end'); $('#parameter-fields').hidden=true; drawLabels();
};
$('#editor-form').onsubmit=async event=>{
  event.preventDefault(); const save=$('#save'); save.disabled=true; message('#editor-error');
  try { await api(state.editor.collection,readEditor()); await load(); $('#editor').close(); }
  catch(error){message('#editor-error',error.message);} finally {save.disabled=false;}
};
$('#cancel-run').onclick=()=>$('#parameters').close();
$('#parameters-form').onsubmit=async event=>{
  event.preventDefault(); const submit=$('button[type="submit"]',event.currentTarget); submit.disabled=true;
  try { const {collection,item,where}=state.parameters; await run(collection,item,Object.fromEntries(new FormData(event.currentTarget)),where); $('#parameters').close(); }
  catch(error){message('#parameters-error',error.message);} finally{submit.disabled=false;}
};

function showLogin(){state.ready=false;$('#workspace').hidden=true;$('#login').hidden=false;}
async function enter(){
  await load(); $('#workspace').hidden=false; $('#login').hidden=true; state.ready=true;
  if(!state.panes.length) state.model.apps.slice(0,2).forEach(app=>addPane(app.id));
  await terminals();
  const retained=sessionStorage.getItem('workbenchTerminal');
  const view=sessionStorage.getItem('workbenchView');
  if(['terminal','logs','library','servers'].includes(view))show(view);
  else if(!state.model.connections.length)show('servers');
  if(retained && state.terminals.some(term=>term.id===retained))attach(retained,sessionStorage.getItem('workbenchTerminalPlacement')==='logs');
}
$('#login-form').onsubmit=async event=>{
  event.preventDefault();const submit=$('button[type="submit"]',event.currentTarget);submit.disabled=true;message('#login-error');
  try{await api('login',{username:$('#username').value,password:$('#password').value});await enter();}
  catch(error){message('#login-error',error.message);}finally{$('#password').value='';submit.disabled=false;}
};
enter().catch(showLogin);
setInterval(()=>{if(state.ready)void loadLogs();},2800);
