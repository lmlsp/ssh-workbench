'use strict';

const $ = (selector, root=document) => root.querySelector(selector);
const $$ = (selector, root=document) => Array.from(root.querySelectorAll(selector));
const state = {view:'terminal', model:{servers:[],connections:[],commands:[],flows:[]}, editor:null, side:'flows'};
const activePhases = ['connecting','switching','dispatch','running','between','waiting','paused','stopping'];
const workspace = new TerminalWorkspace();
const workspaceListeners = new Set();
let lastWorkspaceState;
let confirmTransferClose;
const passwordControls={
  commands:{input:'#edit-secret',toggle:'#toggle-secret',clear:'#clear-secret',endpoint:'password',hint:'例如 SSH 或 su 密码'},
  connections:{input:'#edit-ssh-password',toggle:'#toggle-ssh-password',clear:'#clear-ssh-password',endpoint:'connection-password',hint:'使用 SSH 密钥时可留空'},
};

async function api(path,data,signal) {
  const options={signal};
  if(data!==undefined) Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const response=await fetch('/api/'+path,options);
  const body=await response.json();
  if(!response.ok) throw Object.assign(new Error(body.error),{status:response.status});
  return body;
}

function message(selector,text='') { const node=$(selector); node.textContent=text; node.hidden=!text; }
function actionNotice(text) { message(state.view==='terminal'?'#sidebar-notice':'#notice',text); }
function byId(collection,id) { return state.model[collection].find(row=>row.id===id); }
function button(label,click) { const node=document.createElement('button'); node.type='button'; node.textContent=label; node.onclick=click; return node; }
function select(node,options,current=node.value) {
  node.replaceChildren(...options.map(([id,label])=>new Option(label,id)));
  if(options.some(([id])=>id===current)) node.value=current;
}
function connectionOption(connection) {
  const via=connection.via?` · 经 ${connection.via}`:'';
  return [connection.id,`${connection.name} · ${connection.user}${via}`];
}
function selectedConnection() {
  const active=workspace.active();
  if(active)return byId('connections',active.connectionId)||(active.packet?.endpoint?.kind==='nested'?active.packet.endpoint:undefined);
  return byId('connections',$('#terminal-connection').value)||state.model.connections.find(item=>item.kind==='local');
}
function byServer(collection,server) { return state.model[collection].filter(item=>byId('connections',item.connection)?.server===server); }
function currentServer() {
  const connection=selectedConnection();
  return byId('servers',connection?.server)||(connection?.kind==='nested'?connection:undefined);
}

function workspaceState() {
  const terminal=workspace.active(), context=terminal?.packet?.context;
  return {
    connections:state.model.connections, connection:terminal?.connectionId, side:state.side,
    workspace:state.view==='terminal'&&!$('#workspace').hidden,
    terminalCount:workspace.panels.size,
    openCount:[...workspace.panels.values()].filter(panel=>!panel.meta.closed).length,
    tiled:workspace.tiled&&workspace.panels.size>1,
    terminalName:terminal?.connectionName, user:context?.user, directory:context?.directory,
    terminalId:terminal?.meta.id, fileTransfer:terminal?.packet?.fileTransfer,
    filesReady:Boolean(terminal?.packet?.ready&&!terminal.meta.closed
      &&!activePhases.includes(terminal.packet.run?.phase)&&!terminal.packet.fileTransfer),
    terminalStatus:terminal?(terminal.meta.closed?'已断开':!context?'正在连接':terminal.packet.ready?'就绪':'终端忙碌'):'',
  };
}

function publishWorkspaceState() {
  if(!workspaceListeners.size)return;
  const snapshot=workspaceState();
  if(lastWorkspaceState&&Object.keys(snapshot).every(key=>snapshot[key]===lastWorkspaceState[key]))return;
  lastWorkspaceState=snapshot;
  for(const listener of workspaceListeners)listener(snapshot);
}
function endpoint(server) {
  if(server.kind==='local')return `${server.host} · ${server.user}`;
  const host=server.host.includes(':')?`[${server.host}]`:server.host;
  return `${host}:${server.port}${server.via?` · 经 ${server.via}`:''}`;
}

function editInTerminal(collection,item={}) {
  const active=workspace.active(), connection=selectedConnection();
  const nested=connection?.kind==='nested'&&!byId('connections',connection.id)?connection:undefined;
  openEditor(collection,{connection:connection?.id,user:active?.packet?.context?.user,...item,
    nested,terminal:nested?active.meta.id:undefined});
}

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
  if(manage && collection==='flows') {
    const copy=button('复制',()=>copyFlow(item.id,copy));
    actions.splice(1,0,copy);
  }
  actions.push(button('执行',()=>askRun(collection,item)));
  return row(item.name,detail,actions);
}

function renderSidebar() {
  const server=currentServer(), active=workspace.active();
  $('#sidebar-title').textContent=server?.name || active?.connectionName || '当前服务器';
  $('#sidebar-endpoint').textContent=server?endpoint(server):active?'已保存的连接已移除':'选择一个 SSH 连接';
  const user=active?.packet?.context?.user;
  message('#sidebar-target',active ? `终端 ${active.ordinal}${active.meta.closed?' · 已断开':user?' · '+user:''}` : '');
  $$('[data-side]').forEach(node=>node.setAttribute('aria-pressed',String(node.dataset.side===state.side)));
  publishWorkspaceState();
  const root=$('#sidebar-items');
  if(state.side==='history') {
    const history=active?.packet?.history || [];
    root.replaceChildren(...history.map(entry=>{
      const li=document.createElement('li'),code=document.createElement('code'); code.textContent=entry.script; li.append(code);
      if(entry.switchTo) {
        const note=document.createElement('p'); note.textContent=`已切换到 ${entry.switchTo}`; li.append(note);
      }
      if(selectedConnection())li.append(button('保存为快捷指令',()=>editInTerminal('commands',{user:entry.user,directory:entry.directory,script:entry.script})));
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
    item.append(button(collection==='flows'?'新建自动化流程':'新建快捷指令',()=>editInTerminal(collection)));
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
  const options=state.model.connections.filter(connection=>!connection.kind).map(connectionOption), selector=$('#library-server');
  const local=state.model.connections.find(item=>item.kind==='local');
  const openLocal=$('#open-local-terminal');
  openLocal.hidden=!local;
  if(local)openLocal.onclick=()=>connect(local.id);
  openLocal.classList.toggle('primary',!options.length);
  $('#head-status').textContent=`${state.model.servers.length} 台服务器`;
  select($('#terminal-connection'),options);
  const hasConnections=state.model.connections.length>0;
  $('#terminal-empty').hidden=hasConnections;
  $('#servers-empty').hidden=hasConnections;
  $('#attach-form').hidden=Boolean(local)&&!options.length;
  $('#attach-form button[type="submit"]').disabled=!options.length;
  select(selector,state.model.servers.map(server=>[server.id,server.name]),selector.value||currentServer()?.id||'');
  $('#server-list').replaceChildren(...state.model.servers.map(server=>{
    const section=document.createElement('section'); section.className='server-group'; section.dataset.server=server.id;
    const header=document.createElement('header'), title=document.createElement('div'), heading=document.createElement('h2'), address=document.createElement('p');
    heading.textContent=server.name; address.textContent=endpoint(server); title.append(heading,address);
    const tools=document.createElement('div'); tools.className='actions';
    if(server.kind==='local') {
      tools.append(button('打开本机终端',()=>connect(server.connections[0])));
      header.append(title,tools);section.append(header);return section;
    }
    if(server.kind!=='nested')tools.append(button('添加登录账号',()=>openEditor('connections',{host:server.host,port:server.port})));
    tools.append(deleteButton('删除服务器','servers',server.id));
    header.append(title,tools);
    const list=document.createElement('ul'); list.className='items';
    list.append(...server.connections.map(id=>{const item=byId('connections',id); return row(item.name,item.passwordSet?`${item.user} · 已保存密码`:item.user,[
      ...(server.kind==='nested'?[]:[button('连接',()=>connect(item.id))]),
      button('编辑',()=>openEditor('connections',item)),deleteButton('删除连接','connections',item.id),
    ]);}));
    section.append(header,list); return section;
  }));
  renderLibrary(); renderSidebar();
}

async function load(){ state.model=await api('state'); renderLists(); }

function show(view) {
  if(state.view!==view) {
    window.scrollTo(0,0);
    message('#notice'); message('#sidebar-notice'); setSidebarOpen(false);
  }
  state.view=view; sessionStorage.setItem('workbenchView',view);
  $$('.view').forEach(node=>{node.hidden=node.id!=='view-'+view;});
  $$('.navigation button').forEach(node=>node.dataset.view===view?node.setAttribute('aria-current','page'):node.removeAttribute('aria-current'));
  workspace.visible=view==='terminal'; workspace.layout();
  if(view==='run-log')void loadRunLog();
}

function setSidebarOpen(open,focus=false) {
  const drawer=$('#terminal-sidebar');
  drawer.dataset.open=String(open);
  const toggle=$('#sidebar-toggle');
  toggle.setAttribute('aria-expanded',String(open));
  if(focus)(open?$('#sidebar-hide'):toggle).focus();
}

async function connect(id) {
  message('#notice');
  try{
    const opened=await api('terminal',{connection:id});
    if(!await workspace.refresh())return;
    show('terminal'); workspace.focus(opened.id,true);
  }
  catch(error){message('#notice',error.message);}
}

async function execute(data) {
  actionNotice('');
  const result=await api('run',data);
  show('terminal'); setSidebarOpen(false); workspace.focus(result.id,true);
}

function askRun(collection,item) {
  const panel=workspace.active();
  if(!panel || panel.meta.closed) return actionNotice('请先选中一个已连接的 SSH 终端。');
  if(panel.connectionId!==item.connection){
    const connection=byId('connections',item.connection);
    return actionNotice(`请选择「${connection?.name || '对应 SSH 连接'}」的终端执行。`);
  }
  const isFlow=collection==='flows';
  const steps=(isFlow?item.steps.map(id=>byId('commands',id)):[item]).map((step,index)=>({
    ...step,group:item.stepGroups?.[index]||'',wait:item.stepWaits?.[index]??0,savedWait:item.stepWaits?.[index]??0,
  }));
  const groups=new Map([['',new Map()]]);
  steps.forEach(step=>{
    const group=step.group;
    if(!groups.has(group))groups.set(group,new Map());
    for(const [key,label] of Object.entries(step?.params||{}))groups.get(group).set(key,label);
  });
  const data={kind:isFlow?'flow':'command',id:item.id,terminal:panel.meta.id,revision:item.revision};
  if(!isFlow && !groups.get('').size){void execute({...data,params:{}}).catch(error=>actionNotice(error.message));return;}
  state.parameters={data,steps,connection:item.connection,submitting:false};
  $('#parameters-title').textContent=item.name;
  const location=panel.packet?.endpoint||byId('connections',panel.connectionId);
  $('#parameters-terminal').textContent=[`终端 ${panel.ordinal}`,panel.connectionName,location?endpoint(location):'',`执行用户 ${steps[0]?.user||'—'}`].filter(Boolean).join(' · ');
  $('#run-preview').hidden=!isFlow;
  $('#run-wait-all').value='0';
  const choices=[...groups.keys()].filter(Boolean);
  $('#run-groups').hidden=!choices.length;
  $('#run-group-options').replaceChildren(...choices.map(group=>{
    const wrap=document.createElement('label'), input=document.createElement('input');
    input.type='checkbox';input.value=group;input.onchange=updateParameterGroups;
    wrap.append(input,group);return wrap;
  }));
  $('#parameters-inputs').replaceChildren(...[...groups].filter(([,fields])=>fields.size).map(([group,fields])=>{
    const section=document.createElement('fieldset');section.className='parameter-group';section.dataset.group=group;
    const legend=document.createElement('legend');legend.textContent=group||'运行参数';section.append(legend);
    for(const [key,label] of fields){
      const wrap=document.createElement('label'), input=document.createElement('input');
      wrap.textContent=label;input.name=key;input.required=true;input.maxLength=4000;wrap.append(input);section.append(wrap);
    }
    return section;
  }));
  if(state.view!=='terminal')show('terminal');
  updateParameterGroups();
  message('#parameters-error');$('#parameters').showModal();
}

function selectedGroups() { return $$('#run-group-options input:checked').map(input=>input.value); }

function stepsForGroups(steps, selected) {
  return steps.filter(step=>!step.group||selected.includes(step.group));
}

function waitInput(step, label) {
  const wrap=document.createElement('label'), input=document.createElement('input');
  wrap.className='step-wait';
  input.type='number';input.min='0';input.step='1';input.value=step.wait;
  input.setAttribute('aria-label',label);
  input.oninput=()=>{step.wait=input.value===''?0:input.valueAsNumber;};
  wrap.append(input,'秒');
  return wrap;
}

function updateParameterGroups() {
  const selected=selectedGroups();
  for(const section of $$('#parameters-inputs fieldset')){
    section.hidden=Boolean(section.dataset.group)&&!selected.includes(section.dataset.group);
    section.disabled=section.hidden;
  }
  if(!$('#run-preview').hidden){
    const steps=stepsForGroups(state.parameters.steps, selected);
    const awaitingSelection=!$('#run-groups').hidden&&!selected.length;
    $('#run-preview-caption').textContent=awaitingSelection
      ? '选择分组后查看本次步骤' : `本次执行 ${steps.length} 步`;
    $('#run-wait-bulk').hidden=awaitingSelection||steps.length<2;
    $('#run-preview-steps').replaceChildren(...steps.map((step,index)=>{
      const item=document.createElement('li'),detail=document.createElement('small');
      detail.textContent=[step.group,step.directory].filter(Boolean).join(' · ');
      item.append(step.name||'未知指令',detail.textContent?' · ':'',detail);
      if(index+1<steps.length)item.append(waitInput(step,`本次第 ${index+1} 步执行后等待（秒）`));
      return item;
    }));
    $('#run-preview-steps').hidden=awaitingSelection;
  }
  updateRunAvailability();
}

function updateRunAvailability() {
  const preview=state.parameters;
  if(!preview)return;
  const panel=workspace.panels.get(preview.data.terminal), packet=panel?.packet, run=packet?.run;
  const running=activePhases.includes(run?.phase);
  preview.replaceRun=null;
  let blocked='', hint='';
  if(!panel || panel.meta.closed || panel.connectionId!==preview.connection)blocked='执行位置已变动，请重新选中终端。';
  else if(running && run.phase==='paused' && packet.ready && run.connection===preview.connection){
    preview.replaceRun=run.id;
    hint=`上一轮「${run.name}」已暂停。可以退出上一轮并执行本次流程。`;
  }else if(running){
    const step=run.steps[run.index]?.name;
    blocked=`「${run.name}」尚未结束${step?'，当前步骤「'+step+'」':''}。请等步骤返回或先退出自动化。`;
  }else if(!packet?.ready)blocked='等待终端返回到命令提示符。';
  message('#parameters-blocked', blocked||hint);
  const submit=$('#parameters-form button[type="submit"]');
  submit.textContent=preview.replaceRun?'退出上一轮并执行':'开始执行';
  submit.disabled=preview.submitting||Boolean(blocked)||!$('#run-groups').hidden&&!selectedGroups().length;
}

const runStatus = {
  pending:'待执行',connecting:'准备执行',switching:'切换用户',dispatch:'准备执行',running:'执行中',between:'执行中',waiting:'倒计时中',
  succeeded:'通过',paused:'已暂停',failed:'失败',ended:'已结束',interrupted:'已中断',disconnected:'待核对',stopping:'正在切换',
  exited:'已退出自动化',detached:'已交给终端',manual:'手工完成','needs-review':'待核对',
};

async function loadRunLog(before=null) {
  const refresh=$('#refresh-run-log'), more=$('#more-run-log');
  if(refresh.disabled)return;
  refresh.disabled=true;more.disabled=true;
  try {
    const data=await api(before?'run-log?before='+before:'run-log');
    const list=$('#run-log-list');
    if(!before)list.replaceChildren();
    for(const run of data.runs){
      const item=document.createElement('li'), details=document.createElement('details'), summary=document.createElement('summary');
      const title=document.createElement('strong'), outcome=document.createElement('span'), date=document.createElement('time');
      title.textContent=run.name;outcome.dataset.status=run.phase;outcome.textContent=runStatus[run.phase];
      date.dateTime=new Date(run.startedAt).toISOString();date.textContent=new Date(run.startedAt).toLocaleString();
      summary.append(title,' · ',outcome,date);
      const target=document.createElement('p'), groups=[...new Set(run.steps.map(step=>step.group).filter(Boolean))];
      target.textContent=[run.kind==='flow'?'流程':'快捷指令',run.target.name,run.target.port?endpoint(run.target):run.target.host,
        run.user,groups.join('、')].filter(Boolean).join(' · ');
      const steps=document.createElement('ol');steps.className='run-log-steps';
      for(const step of run.steps){
        const row=document.createElement('li'), status=document.createElement('span'), extra=document.createElement('small');
        status.dataset.status=step.status;status.textContent=runStatus[step.status];
        extra.textContent=[step.group,step.directory,step.exitCode!=null?`返回码 ${step.exitCode}`:'',
          step.attempts>1?`执行 ${step.attempts} 次`:'',step.waitSeconds?`设置等待 ${step.waitSeconds} 秒`:''].filter(Boolean).join(' · ');
        row.append(step.name,' · ',status,' ',extra);steps.append(row);
      }
      const last=document.createElement('p');last.textContent=run.message;
      details.append(summary,target,last,steps);item.append(details);list.append(item);
    }
    $('#run-log-empty').hidden=Boolean(list.children.length);
    more.hidden=!data.before;more.onclick=()=>loadRunLog(data.before);
  } catch(error){if(state.view==='run-log')actionNotice(error.message);}
  finally{refresh.disabled=false;more.disabled=false;}
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

async function copyFlow(id,control) {
  control.disabled=true;
  actionNotice('');
  try {
    const {flows}=await api('state');
    const source=flows.find(item=>item.id===id);
    if(!source)throw new Error('这条流程已不存在，请刷新后重试。');
    const names=new Set(flows.map(item=>item.name));
    const stem=source.name.replace(/（副本(?: \d+)?）$/,'') || source.name;
    let name;
    for(let number=1;;number++) {
      const suffix=number===1?'（副本）':`（副本 ${number}）`;
      name=Array.from(stem).slice(0,60-suffix.length).join('')+suffix;
      if(!names.has(name))break;
    }
    const duplicate=await api('flows',{name,connection:source.connection,app:source.app||'',type:source.type||'',
      steps:source.steps,stepGroups:source.stepGroups,stepWaits:source.stepWaits});
    await load();
    openEditor('flows',duplicate);
  } catch(error) { actionNotice(error.message); }
  finally { control.disabled=false; }
}

function showContext() {
  const connection=$('#edit-connection').value;
  const first=byId('commands',state.editor.steps[0]?.id);
  const choices=state.model.commands.filter(item=>item.connection===connection&&!item.switchTo&&(!first||item.user===first.user));
  select($('#step-choice'),choices.map(item=>[item.id,`${item.name} · ${item.user}`]));
  $('#add-step').disabled=!choices.length;
}

function drawSteps() {
  showContext();
  $('#apply-edit-wait').disabled=!state.editor.steps.length;
  $('#step-list').replaceChildren(...state.editor.steps.map((entry,index)=>{
    const step=byId('commands',entry.id),li=document.createElement('li'); li.textContent=step?.name || '未知指令';
    li.className='flow-step';
    const handle=document.createElement('button');handle.type='button';handle.className='step-grip';
    handle.title='拖动或用上下方向键排序';handle.setAttribute('aria-label',`调整第 ${index+1} 步：${li.textContent}`);
    const number=document.createElement('span'),name=document.createElement('span');
    number.textContent=String(index+1);number.className='step-order';name.textContent=li.textContent;name.className='step-name';
    const group=document.createElement('input');
    group.className='step-group';group.value=entry.group;group.maxLength=32;group.placeholder='公共步骤';
    group.setAttribute('list','flow-groups');group.setAttribute('aria-label',`第 ${index+1} 步分组`);
    group.oninput=()=>{entry.group=group.value.trim();};
    group.onchange=()=>{group.value=entry.group;drawGroupOptions();};
    bindStepDrag(handle,li,index);
    li.replaceChildren(handle,number,name,group,waitInput(entry,`第 ${index+1} 步执行后等待（秒）`),
      button('移除',()=>{state.editor.steps.splice(index,1);drawSteps();}));return li;
  }));
  drawGroupOptions();
}

function drawGroupOptions() {
  const groups=new Set(state.editor.steps.map(step=>step.group).filter(Boolean));
  $('#flow-groups').replaceChildren(...Array.from(groups,name=>new Option(name,name)));
}

function moveStep(from,to) {
  const steps=state.editor?.steps;
  if(!steps || to===from || to<0 || to>=steps.length)return;
  steps.splice(to,0,steps.splice(from,1)[0]);
  drawSteps();
  $$('#step-list .step-grip')[to]?.focus();
}

function bindStepDrag(handle,row,index) {
  let drag;
  const list=$('#step-list'), editor=$('#editor');
  handle.onkeydown=event=>{
    const offset=event.key==='ArrowUp'?-1:event.key==='ArrowDown'?1:0;
    if(offset){event.preventDefault();moveStep(index,index+offset);}
  };

  function updateMarker() {
    const bounds=list.getBoundingClientRect();
    const rows=[...list.children];
    let gap=null;
    if(drag.x>=bounds.left-18 && drag.x<=bounds.right+18) {
      const index=rows.findIndex(node=>{const box=node.getBoundingClientRect();return drag.y<box.top+box.height/2;});
      gap=index<0?rows.length:index;
    }
    if(drag.gap===gap)return;
    drag.marker?.classList.remove('drop-before','drop-after');
    drag.gap=gap;
    drag.marker=gap===null?null:rows[Math.min(gap,rows.length-1)];
    drag.marker?.classList.add(gap===rows.length?'drop-after':'drop-before');
  }

  function scrollWhileDragging() {
    if(!drag)return;
    const bounds=editor.getBoundingClientRect();
    const top=bounds.top+44-drag.y, bottom=drag.y-bounds.bottom+44;
    const speed=top>0?-Math.min(14,top/3):bottom>0?Math.min(14,bottom/3):0;
    if(speed) {
      const previous=editor.scrollTop;editor.scrollTop+=speed;
      if(editor.scrollTop!==previous)updateMarker();
    }
    drag.frame=requestAnimationFrame(scrollWhileDragging);
  }

  handle.onpointerdown=event=>{
    if(event.button!==0 || !event.isPrimary)return;
    event.preventDefault();handle.setPointerCapture(event.pointerId);
    drag={id:event.pointerId,x:event.clientX,y:event.clientY,gap:null,marker:null};
    row.classList.add('step-dragging');updateMarker();scrollWhileDragging();
  };
  handle.onpointermove=event=>{
    if(drag?.id!==event.pointerId)return;
    drag.x=event.clientX;drag.y=event.clientY;updateMarker();
  };
  function stop(event,drop=false) {
    if(drag?.id!==event.pointerId)return;
    const {gap,marker,frame}=drag;drag=null;
    cancelAnimationFrame(frame);marker?.classList.remove('drop-before','drop-after');row.classList.remove('step-dragging');
    if(drop && gap!==null)moveStep(index,gap-(gap>index?1:0));
  }
  handle.onpointerup=event=>stop(event,true);
  handle.onpointercancel=event=>stop(event);
  handle.onlostpointercapture=event=>stop(event);
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
  state.editor={collection,id:item.id,connection:item.connection,app:item.app||'',
    steps:(item.steps||[]).map((id,index)=>({id,group:item.stepGroups?.[index]||'',wait:item.stepWaits?.[index]??0})),params:{...item.params},
    terminal:item.terminal,nested:item.nested,passwordSet:!!item.passwordSet,secretTouched:false};
  const defaultConnection=state.view==='library'?byId('servers',$('#library-server').value)?.connections[0]:selectedConnection()?.id;
  $('#editor-title').textContent=(item.id?'编辑':'新建')+(server?' SSH 连接':command?'快捷指令':'自动化流程');
  const nestedServer=server&&item.kind==='nested';
  $('#server-fields').hidden=!server||nestedServer;$('#ssh-password-fields').hidden=!server||nestedServer;$('#context-fields').hidden=server;$('#command-fields').hidden=!command;$('#flow-fields').hidden=!flow;
  $('#parameter-fields').hidden=true;
  const options=state.model.connections.map(connectionOption);
  if(item.nested)options.push(connectionOption(item.nested));
  select($('#edit-connection'),options,item.connection||defaultConnection||'');
  $('#edit-name').value=item.name||'';$('#edit-host').value=item.host||'';$('#edit-port').value=item.port||22;$('#edit-login-user').value=item.user||'';
  $('#edit-type').value=item.type||'';$('#edit-user').value=item.user||byId('connections',$('#edit-connection').value)?.user||'';
  $('#edit-directory').value=item.directory||'';$('#edit-script').value=item.script||'';
  $('#edit-wait-all').value='0';
  $('#edit-followup-prompt').value=item.followup?.prompt||'';
  $('#edit-followup-mode').value=item.followup?.mode||'auto';updateFollowupFields();
  const fields=passwordControls[collection];
  if(fields) {
    const input=$(fields.input);input.value='';input.type='password';input.placeholder=item.passwordSet?'••••••••  已保存':fields.hint;
    $(fields.toggle).textContent='显示';$(fields.clear).hidden=!item.passwordSet;
  }
  drawSteps();drawLabels();message('#editor-error');$('#editor').showModal();
}

function readEditor() {
  const editor=state.editor,value={id:editor.id,name:$('#edit-name').value};
  if(editor.secretTouched)value.password=$(passwordControls[editor.collection].input).value;
  if(editor.collection==='connections')return {...value,host:$('#edit-host').value,port:Number($('#edit-port').value),user:$('#edit-login-user').value};
  value.connection=$('#edit-connection').value;
  if(value.connection===editor.nested?.id)value.terminal=editor.terminal;
  value.app=value.connection===editor.connection?editor.app:'';value.type=$('#edit-type').value;
  if(editor.collection==='flows')return {...value,steps:editor.steps.map(step=>step.id),
    stepGroups:editor.steps.map(step=>step.group),stepWaits:editor.steps.map(step=>step.wait)};
  return {...value,script:$('#edit-script').value,user:$('#edit-user').value,directory:$('#edit-directory').value,params:editor.params,
    followup:{prompt:$('#edit-followup-prompt').value,mode:$('#edit-followup-mode').value}};
}

function updateFollowupFields() {
  const custom=Boolean($('#edit-followup-prompt').value.trim()),mode=$('#edit-followup-mode');
  mode.disabled=!custom;
  $('#followup-hint').hidden=!custom||mode.value!=='auto';
}

$$('[data-view]').forEach(node=>{node.onclick=()=>show(node.dataset.view);});
$$('[data-side]').forEach(node=>{node.onclick=()=>{state.side=node.dataset.side;renderSidebar();};});
$('#attach-form').onsubmit=event=>{event.preventDefault();void connect($('#terminal-connection').value);};
$('#terminal-connection').onchange=renderSidebar;
$('#toggle-split').onclick=()=>workspace.toggleTiles();
$('#sidebar-toggle').onclick=()=>setSidebarOpen($('#terminal-sidebar').dataset.open!=='true',true);
$('#sidebar-hide').onclick=()=>setSidebarOpen(false,true);
document.addEventListener('keydown',event=>{
  if(event.key==='Escape' && !event.defaultPrevented && !$('dialog[open]') && $('#terminal-sidebar').dataset.open==='true')setSidebarOpen(false,true);
});
$('#library-server').onchange=renderLibrary;
$('#new-command').onclick=()=>openEditor('commands');$('#new-flow').onclick=()=>openEditor('flows');
$('#add-server').onclick=()=>openEditor('connections');
$('#edit-connection').onchange=()=>{
  if(state.editor.collection==='flows'){state.editor.steps=[];drawSteps();}
  if(state.editor.collection==='commands')$('#edit-user').value=byId('connections',$('#edit-connection').value)?.user||'';
};
$('#add-step').onclick=()=>{const choice=$('#step-choice').value;if(choice&&state.editor.steps.length<24){state.editor.steps.push({id:choice,group:'',wait:0});drawSteps();}};
$('#apply-edit-wait').onclick=()=>{
  const input=$('#edit-wait-all');
  if(!input.reportValidity())return;
  for(const step of state.editor.steps)step.wait=input.value===''?0:input.valueAsNumber;
  drawSteps();
};
$('#apply-run-wait').onclick=()=>{
  const input=$('#run-wait-all');
  if(!input.reportValidity())return;
  const steps=stepsForGroups(state.parameters.steps,selectedGroups());
  for(const step of steps.slice(0,-1))step.wait=input.value===''?0:input.valueAsNumber;
  updateParameterGroups();
};
$('#cancel-edit').onclick=$('#editor-close').onclick=()=>$('#editor').close();
$('#editor').addEventListener('close',()=>{Object.values(passwordControls).forEach(fields=>{$(fields.input).value='';});state.editor=null;});
for(const fields of Object.values(passwordControls)){
  const field=$(fields.input),toggle=$(fields.toggle);
  field.oninput=()=>{state.editor.secretTouched=true;};
  $(fields.clear).onclick=()=>{field.value='';field.placeholder='保存后清除密码';state.editor.secretTouched=true;};
  toggle.onclick=async()=>{
    if(field.type==='text'){field.type='password';toggle.textContent='显示';return;}
    const editing=state.editor;
    try {
      if(!field.value&&editing.passwordSet&&!editing.secretTouched){
        const {password}=await api(fields.endpoint,{id:editing.id});
        if(state.editor!==editing || editing.secretTouched)return;
        field.value=password||'';
      }
      field.type='text';toggle.textContent='隐藏';
    }catch(error){message('#editor-error',error.message);}
  };
}
$('#edit-script').oninput=drawLabels;
$('#edit-followup-prompt').oninput=$('#edit-followup-mode').onchange=updateFollowupFields;
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
$('#refresh-run-log').onclick=()=>loadRunLog();
$('#parameters-form').onsubmit=async event=>{
  event.preventDefault();
  const preview=state.parameters;
  if(!preview||$('button[type="submit"]',event.currentTarget).disabled)return;
  preview.submitting=true;
  updateRunAvailability();
  const fields=Object.fromEntries($$('#parameters-inputs fieldset:not([disabled])').map(section=>[
    section.dataset.group,Object.fromEntries($$('input',section).map(input=>[input.name,input.value])),
  ]));
  const { '': params={}, ...groupParams }=fields;
  const groups=selectedGroups(), timedSteps=stepsForGroups(preview.steps,groups).slice(0,-1);
  const stepWaits=preview.steps.map(step=>timedSteps.includes(step)?step.wait:step.savedWait);
  try{await execute({...preview.data,params,groups,groupParams,stepWaits,replaceRun:preview.replaceRun});$('#parameters').close();}
  catch(error){message('#parameters-error',error.message);}finally{preview.submitting=false;updateRunAvailability();}
};
$('#cancel-delete').onclick=()=>$('#delete-dialog').close();
$('#delete-form').onsubmit=async event=>{
  event.preventDefault();const submit=$('#confirm-delete');if(submit.hidden)return;submit.disabled=true;
  try{await api('delete',state.deletion);$('#delete-dialog').close();await load();await workspace.refresh();}
  catch(error){message('#delete-error',error.message);}finally{submit.disabled=false;}
};

function showLogin(){
  $$('dialog[open]').forEach(dialog=>dialog.close());workspace.visible=false;
  $('#workbench-navigation').hidden=true;$('#workspace').hidden=true;$('#login').hidden=false;
  workspace.layout();
}
async function enter(){
  await load();await workspace.refresh(true);
  $('#workspace').hidden=false;$('#workbench-navigation').hidden=false;$('#login').hidden=true;
  const view=sessionStorage.getItem('workbenchView');
  show(['terminal','library','servers','run-log'].includes(view)?view:state.model.connections.length?'terminal':'servers');
}
$('#login-form').onsubmit=async event=>{
  event.preventDefault();const submit=$('button[type="submit"]',event.currentTarget);submit.disabled=true;message('#login-error');
  try{await api('login',{username:$('#username').value,password:$('#password').value});await enter();}
  catch(error){message('#login-error',error.message);}finally{$('#password').value='';submit.disabled=false;}
};
window.workbenchControls=Object.freeze({
  subscribe(listener){
    workspaceListeners.add(listener);
    listener(workspaceState());
    return ()=>workspaceListeners.delete(listener);
  },
  connect,
  request:api,
  onTransferClose(handler){confirmTransferClose=handler;},
  addConnection(){openEditor('connections');},
  toggleTiles(){workspace.toggleTiles();},
  showSide(side){
    if(!['commands','flows','history'].includes(side))return;
    state.side=side;
    renderSidebar();
  },
  setTerminalAppearance(colors,variables){workspace.setAppearance(colors,variables);},
});
window.dispatchEvent(new Event('workbench:controls-ready'));
enter().catch(showLogin);
