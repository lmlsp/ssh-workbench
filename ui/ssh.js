'use strict';

const $ = (selector, root=document) => root.querySelector(selector);
const $$ = (selector, root=document) => Array.from(root.querySelectorAll(selector));
const state = {view:'terminal', model:{servers:[],connections:[],commands:[],flows:[]}, editor:null, side:'flows'};
const activePhases = ['connecting','switching','dispatch','running','between','paused'];
const workspace = new TerminalWorkspace();

async function api(path,data,signal) {
  const options={signal};
  if(data!==undefined) Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const response=await fetch('/api/'+path,options);
  const body=await response.json();
  if(!response.ok) throw Object.assign(new Error(body.error),{status:response.status});
  return body;
}

function message(selector,text='') { const node=$(selector); node.textContent=text; node.hidden=!text; }
function byId(collection,id) { return state.model[collection].find(row=>row.id===id); }
function button(label,click) { const node=document.createElement('button'); node.type='button'; node.textContent=label; node.onclick=click; return node; }
function select(node,options,current=node.value) {
  node.replaceChildren(...options.map(([id,label])=>new Option(label,id)));
  if(options.some(([id])=>id===current)) node.value=current;
}
function connectionOptions() {
  return state.model.connections.map(connection=>[connection.id,`${connection.name} · ${connection.user}`]);
}
function selectedConnection() { return byId('connections',workspace.active()?.meta.connection || $('#terminal-connection').value); }
function byServer(collection,server) { return state.model[collection].filter(item=>byId('connections',item.connection)?.server===server); }
function currentServer() { return byId('servers',workspace.active()?.meta.server || selectedConnection()?.server); }
function endpoint(server) { return `${server.host}:${server.port}`; }

function row(title,detail,actions) {
  const li=document.createElement('li'), words=document.createElement('div');
  const label=document.createElement('strong'), subtitle=document.createElement('p');
  label.textContent=title; subtitle.textContent=detail; words.append(label,subtitle);
  const buttons=document.createElement('div'); buttons.className='actions'; buttons.append(...actions);
  li.append(words,buttons); return li;
}

function deleteButton(label,kind,id) {
  const node=button(label,()=>askDelete(kind,id)); node.className='danger-link'; return node;
}

function operationRow(collection,item,manage=false) {
  const connection=byId('connections',item.connection);
  const steps=collection==='flows'? item.steps.map(id=>byId('commands',id)).filter(Boolean):[item];
  const identity=item.switchTo ? `${item.user} → ${item.switchTo}` : steps[0]?.user;
  const detail=[connection?.name,identity,item.type,collection==='flows'?`${item.steps.length} 步`:''].filter(Boolean).join(' · ');
  const actions=manage?[button('编辑',()=>openEditor(collection,item)),deleteButton('删除',collection,item.id)]:[];
  actions.push(button('执行',()=>askRun(collection,item)));
  return row(item.name,detail,actions);
}

function renderSidebar() {
  const server=currentServer(), active=workspace.active();
  $('#sidebar-title').textContent=server?.name || active?.meta.connectionName || '当前服务器';
  $('#sidebar-endpoint').textContent=server?endpoint(server):active?'已保存的连接已移除':'选择一个 SSH 连接';
  $$('[data-side]').forEach(node=>node.setAttribute('aria-pressed',String(node.dataset.side===state.side)));
  const root=$('#sidebar-items');
  if(state.side==='history') {
    const history=active?.packet?.history || [];
    root.replaceChildren(...history.map(entry=>{
      const li=document.createElement('li'),code=document.createElement('code'); code.textContent=entry.script; li.append(code);
      if(entry.switchTo) {
        const note=document.createElement('p'); note.textContent=`已切换到 ${entry.switchTo}`; li.append(note);
      }
      if(selectedConnection())li.append(button('保存为快捷指令',()=>openEditor('commands',{connection:active.meta.connection,user:entry.user,directory:entry.directory,script:entry.script})));
      return li;
    }));
    message('#sidebar-empty',history.length?'':'执行过的命令会在终端返回提示符后显示。');
    return;
  }
  const items=server?byServer(state.side,server.id):[];
  root.replaceChildren(...items.map(item=>operationRow(state.side,item)));
  message('#sidebar-empty',items.length?'':server?(state.side==='flows'?'这台服务器还没有自动化流程。':'这台服务器还没有快捷指令。'):'先选择服务器。');
  if(server && !items.length) {
    const collection=state.side==='flows'&&byServer('commands',server.id).some(item=>!item.switchTo)?'flows':'commands';
    const item=document.createElement('li');
    item.append(button(collection==='flows'?'新建自动化流程':'新建快捷指令',()=>openEditor(collection,{connection:selectedConnection()?.id})));
    root.append(item);
  }
}

function renderLibrary() {
  const list=$('#library-server');
  const serverId=list.value;
  for(const [collection,target] of [['flows','#flow-list'],['commands','#command-list']]) {
    const items=byServer(collection,serverId);
    $(target).replaceChildren(...items.map(item=>operationRow(collection,item,true)));
    if(!items.length)$(target).append(row(collection==='flows'?'暂无自动化流程':'暂无快捷指令','',[]));
  }
  $('#new-command').disabled=!serverId;
  $('#new-flow').disabled=!byServer('commands',serverId).some(item=>!item.switchTo);
}

function renderLists() {
  const options=connectionOptions(), selector=$('#library-server');
  $('#head-status').textContent=`${state.model.servers.length} 台服务器`;
  select($('#terminal-connection'),options);
  const hasConnections=options.length>0;
  $('#terminal-empty').hidden=hasConnections;
  $('#servers-empty').hidden=hasConnections;
  $('#attach-form button[type="submit"]').disabled=!hasConnections;
  select(selector,state.model.servers.map(server=>[server.id,server.name]),selector.value||currentServer()?.id||'');
  $('#server-list').replaceChildren(...state.model.servers.map(server=>{
    const section=document.createElement('section'); section.className='server-group'; section.dataset.server=server.id;
    const header=document.createElement('header'), title=document.createElement('div'), heading=document.createElement('h2'), address=document.createElement('p');
    heading.textContent=server.name; address.textContent=endpoint(server); title.append(heading,address);
    const tools=document.createElement('div'); tools.className='actions';
    tools.append(button('添加登录账号',()=>openEditor('connections',{host:server.host,port:server.port})),deleteButton('删除服务器','servers',server.id));
    header.append(title,tools);
    const list=document.createElement('ul'); list.className='items';
    list.append(...server.connections.map(id=>{const item=byId('connections',id); return row(item.name,item.user,[
      button('连接',()=>connect(item.id)),button('编辑',()=>openEditor('connections',item)),deleteButton('删除连接','connections',item.id),
    ]);}));
    section.append(header,list); return section;
  }));
  renderLibrary(); renderSidebar();
}

async function load(){ state.model=await api('state'); renderLists(); }

function show(view) {
  state.view=view; sessionStorage.setItem('workbenchView',view);
  $$('.view').forEach(node=>{node.hidden=node.id!=='view-'+view;});
  $$('.navigation button').forEach(node=>node.dataset.view===view?node.setAttribute('aria-current','page'):node.removeAttribute('aria-current'));
  workspace.visible=view==='terminal'; workspace.layout();
}

async function openTab(path,data) {
  message('#notice');
  const launched=await api(path,data);
  await workspace.refresh();
  show('terminal'); workspace.focus(launched.id,true);
}

async function connect(id) {
  try{await openTab('terminal',{connection:id});}
  catch(error){message('#notice',error.message);}
}

function askRun(collection,item) {
  const steps=collection==='flows'?item.steps.map(id=>byId('commands',id)).filter(Boolean):[item];
  const fields=Object.assign({},...steps.map(step=>step.params));
  const data={kind:collection==='flows'?'flow':'command',id:item.id};
  if(!Object.keys(fields).length){void openTab('run',{...data,params:{}}).catch(error=>message('#notice',error.message));return;}
  state.parameters=data;
  $('#parameters-title').textContent=item.name;
  $('#parameters-inputs').replaceChildren(...Object.entries(fields).map(([key,label])=>{
    const wrap=document.createElement('label'),input=document.createElement('input');
    wrap.textContent=label;input.name=key;input.required=true;input.maxLength=4000;wrap.append(input);return wrap;
  }));
  message('#parameters-error');$('#parameters').showModal();
}

async function askDelete(collection,id) {
  message('#notice');
  try {
    const summary=await api('delete-preview',{collection,id});
    state.deletion={collection,id,fingerprint:summary.fingerprint};
    $('#delete-title').textContent=`删除「${summary.name}」`;
    const labels={connections:'SSH 连接',commands:'快捷指令',flows:'自动化流程',apps:'旧日志配置'};
    const entries=Object.entries(summary.items).filter(([,items])=>items.length);
    const list=$('#delete-items'); list.replaceChildren(...entries.map(([kind,items])=>{
      const li=document.createElement('li'); li.textContent=`${labels[kind]} ${items.length} 项 · ${items.map(item=>item.name).join('、')}`; return li;
    }));
    list.hidden=collection==='commands'||collection==='flows';
    const blocked=summary.busy||summary.references.length>0;
    $('#confirm-delete').hidden=blocked;
    $('#cancel-delete').textContent=blocked?'关闭':'取消';
    $('#delete-message').textContent=collection==='flows'?'只删除保存的流程。':collection==='commands'?'删除这条快捷指令。':'删除以下关联的本机配置。';
    message('#delete-error',summary.references.length?`请先修改引用它的流程：${summary.references.join('、')}`:
      summary.busy?'还有终端任务在运行或等待输入，请先处理完再删除。':'');
    $('#delete-dialog').showModal();
  } catch(error){message('#notice',error.message);}
}

function showContext() {
  const connection=$('#edit-connection').value;
  const first=byId('commands',state.editor.steps[0]);
  const choices=state.model.commands.filter(item=>item.connection===connection&&!item.switchTo&&(!first||item.user===first.user));
  select($('#step-choice'),choices.map(item=>[item.id,`${item.name} · ${item.user}`]));
  $('#add-step').disabled=!choices.length;
}

function drawSteps() {
  showContext();
  $('#step-list').replaceChildren(...state.editor.steps.map((id,index)=>{
    const step=byId('commands',id),li=document.createElement('li'); li.textContent=step?.name || '未知指令';
    const move=delta=>{const rows=state.editor.steps;[rows[index],rows[index+delta]]=[rows[index+delta],rows[index]];drawSteps();};
    const up=button('上移',()=>move(-1)),down=button('下移',()=>move(1));
    up.disabled=index===0;down.disabled=index===state.editor.steps.length-1;
    li.append(up,down,button('移除',()=>{state.editor.steps.splice(index,1);drawSteps();}));return li;
  }));
}

function drawLabels() {
  const matches=[...new Set(Array.from($('#edit-script').value.matchAll(/\{\{([a-zA-Z][a-zA-Z0-9_]{0,31})\}\}/g),match=>match[1]))];
  $('#parameter-labels').replaceChildren(...matches.map(key=>{
    const label=document.createElement('label'),input=document.createElement('input');
    label.textContent=`${key} 的提示`;input.value=state.editor.params[key]||key;input.maxLength=40;
    input.oninput=()=>{state.editor.params[key]=input.value;};label.append(input);return label;
  }));
}

function openEditor(collection,item={}) {
  if($('#editor').open)return;
  const server=collection==='connections',command=collection==='commands',flow=collection==='flows';
  state.editor={collection,id:item.id,connection:item.connection,app:item.app||'',steps:[...(item.steps||[])],params:{...item.params},
    passwordSet:!!item.passwordSet,secretTouched:false};
  const defaultConnection=state.view==='library'?byId('servers',$('#library-server').value)?.connections[0]:selectedConnection()?.id;
  $('#editor-title').textContent=(item.id?'编辑':'新建')+(server?' SSH 连接':command?'快捷指令':'自动化流程');
  $('#server-fields').hidden=!server;$('#context-fields').hidden=server;$('#command-fields').hidden=!command;$('#flow-fields').hidden=!flow;
  $('#parameter-fields').hidden=true;
  select($('#edit-connection'),connectionOptions(),item.connection||defaultConnection||'');
  $('#edit-name').value=item.name||'';$('#edit-host').value=item.host||'';$('#edit-port').value=item.port||22;$('#edit-login-user').value=item.user||'';
  $('#edit-type').value=item.type||'';$('#edit-user').value=item.user||byId('connections',$('#edit-connection').value)?.user||'';
  $('#edit-directory').value=item.directory||'';$('#edit-script').value=item.script||'';
  $('#edit-secret').value='';$('#edit-secret').type='password';$('#edit-secret').placeholder=item.passwordSet?'已保存，输入可更换':'需要 su 时可填写';
  $('#toggle-secret').textContent='显示';$('#clear-secret').hidden=!item.passwordSet;
  drawSteps();drawLabels();message('#editor-error');$('#editor').showModal();
}

function readEditor() {
  const editor=state.editor,value={id:editor.id,name:$('#edit-name').value};
  if(editor.collection==='connections')return {...value,host:$('#edit-host').value,port:Number($('#edit-port').value),user:$('#edit-login-user').value};
  value.connection=$('#edit-connection').value;
  value.app=value.connection===editor.connection?editor.app:'';value.type=$('#edit-type').value;
  if(editor.collection==='flows')return {...value,steps:editor.steps};
  if(editor.secretTouched)value.password=$('#edit-secret').value;
  return {...value,script:$('#edit-script').value,user:$('#edit-user').value,directory:$('#edit-directory').value,params:editor.params};
}

$$('[data-view]').forEach(node=>{node.onclick=()=>show(node.dataset.view);});
$$('[data-side]').forEach(node=>{node.onclick=()=>{state.side=node.dataset.side;renderSidebar();};});
$('#attach-form').onsubmit=event=>{event.preventDefault();void connect($('#terminal-connection').value);};
$('#terminal-connection').onchange=renderSidebar;
$('#toggle-split').onclick=()=>workspace.toggleTiles();
$('#library-server').onchange=renderLibrary;
$('#new-command').onclick=()=>openEditor('commands');$('#new-flow').onclick=()=>openEditor('flows');
$('#add-server').onclick=()=>openEditor('connections');
$('#edit-connection').onchange=()=>{
  if(state.editor.collection==='flows'){state.editor.steps=[];drawSteps();}
  if(state.editor.collection==='commands')$('#edit-user').value=byId('connections',$('#edit-connection').value)?.user||'';
};
$('#add-step').onclick=()=>{const choice=$('#step-choice').value;if(choice&&state.editor.steps.length<24){state.editor.steps.push(choice);drawSteps();}};
$('#cancel-edit').onclick=$('#editor-close').onclick=()=>$('#editor').close();
$('#editor').addEventListener('close',()=>{$('#edit-secret').value='';state.editor=null;});
$('#edit-secret').oninput=()=>{state.editor.secretTouched=true;};
$('#clear-secret').onclick=()=>{$('#edit-secret').value='';$('#edit-secret').placeholder='保存后清除密码';state.editor.secretTouched=true;};
$('#toggle-secret').onclick=async()=>{
  const field=$('#edit-secret'),toggle=$('#toggle-secret');
  if(field.type==='text'){field.type='password';toggle.textContent='显示';return;}
  try {
    if(!field.value&&state.editor.passwordSet&&!state.editor.secretTouched){const {password}=await api('password',{id:state.editor.id});field.value=password||'';}
    field.type='text';toggle.textContent='隐藏';
  }catch(error){message('#editor-error',error.message);}
};
$('#edit-script').oninput=drawLabels;
$('#mark-parameter').onclick=()=>{
  const input=$('#edit-script');let start=input.selectionStart,end=input.selectionEnd;
  if(start===end)return message('#editor-error','先在命令中选中链接或要替换的值。');
  const quote=input.value[start-1];if((quote==='"'||quote==="'")&&input.value[end]===quote){start--;end++;}
  state.editor.selection=[start,end];const url=/https?:|ftp:/.test(input.value.slice(start,end));
  $('#edit-parameter-name').value=url?'buildUrl':`param${Object.keys(state.editor.params).length+1}`;
  $('#edit-parameter-label').value=url?'构建包链接':'';
  message('#editor-error');$('#parameter-fields').hidden=false;$('#edit-parameter-label').focus();
};
$('#insert-parameter').onclick=()=>{
  const key=$('#edit-parameter-name').value.trim();
  if(!/^[a-zA-Z][a-zA-Z0-9_]{0,31}$/.test(key))return message('#editor-error','参数名请用字母、数字或下划线，并用字母开头。');
  state.editor.params[key]=$('#edit-parameter-label').value.trim()||key;
  const [start,end]=state.editor.selection;
  $('#edit-script').setRangeText(`{{${key}}}`,start,end,'end');$('#parameter-fields').hidden=true;drawLabels();
};
$('#editor-form').onsubmit=async event=>{
  event.preventDefault();const save=$('#save');save.disabled=true;message('#editor-error');
  try{await api(state.editor.collection,readEditor());await load();$('#editor').close();}
  catch(error){message('#editor-error',error.message);}finally{save.disabled=false;}
};
$('#cancel-run').onclick=()=>$('#parameters').close();
$('#parameters-form').onsubmit=async event=>{
  event.preventDefault();const submit=$('button[type="submit"]',event.currentTarget);submit.disabled=true;
  try{await openTab('run',{...state.parameters,params:Object.fromEntries(new FormData(event.currentTarget))});$('#parameters').close();}
  catch(error){message('#parameters-error',error.message);}finally{submit.disabled=false;}
};
$('#cancel-delete').onclick=()=>$('#delete-dialog').close();
$('#delete-form').onsubmit=async event=>{
  event.preventDefault();const submit=$('#confirm-delete');if(submit.hidden)return;submit.disabled=true;
  try{await api('delete',state.deletion);$('#delete-dialog').close();await load();await workspace.refresh();}
  catch(error){message('#delete-error',error.message);}finally{submit.disabled=false;}
};

function showLogin(){workspace.visible=false;workspace.layout();$('#workspace').hidden=true;$('#login').hidden=false;}
async function enter(){
  await load();$('#workspace').hidden=false;$('#login').hidden=true;
  const view=sessionStorage.getItem('workbenchView');
  show(['terminal','library','servers'].includes(view)?view:state.model.connections.length?'terminal':'servers');
  await workspace.refresh();
}
$('#login-form').onsubmit=async event=>{
  event.preventDefault();const submit=$('button[type="submit"]',event.currentTarget);submit.disabled=true;message('#login-error');
  try{await api('login',{username:$('#username').value,password:$('#password').value});await enter();}
  catch(error){message('#login-error',error.message);}finally{$('#password').value='';submit.disabled=false;}
};
enter().catch(showLogin);
