'use strict';

class TerminalPanel {
  constructor(meta, workspace, ordinal) {
    this.meta = meta;
    this.workspace = workspace;
    this.ordinal = ordinal;
    this.revision = -1;
    this.packet = null;
    this.pendingInput = '';
    this.inputChain = Promise.resolve();
    this.node = $('#terminal-template').content.firstElementChild.cloneNode(true);
    this.node.dataset.session = meta.id;
    this.host = $('.xterm-host', this.node);
    this.mountPoint = $('.xterm-mount', this.host);
    $('.terminal-index', this.node).textContent=String(ordinal);
    $('.term-name', this.node).textContent = meta.name;
    $('.focus-pane', this.node).onclick = () => this.workspace.focus(this.meta.id, true);
    const close = $('.close-pane', this.node);
    close.onclick = () => this.workspace.close(this.meta.id);
    close.setAttribute('aria-label', `关闭终端 ${ordinal} · ${meta.connectionName}`);
    this.host.addEventListener('focusin', () => this.workspace.focus(this.meta.id));
    $$('[data-action]', this.node).forEach(node => { node.onclick = () => this.control(node.dataset.action); });
    this.applySize();
    this.bindResizeHandle();
  }

  get path() { return `terminal/${this.meta.id}`; }

  open() {
    if (this.terminal) return;
    this.terminal = new Terminal({ fontFamily:'Cascadia Mono, Noto Sans Mono CJK SC, monospace', fontSize:14, cursorBlink:true,
      theme:{background:'#111c2c', foreground:'#e4edf9', cursor:'#abceff', selectionBackground:'#345989'}, scrollback:4500 });
    this.fit = new FitAddon.FitAddon();
    this.terminal.loadAddon(this.fit);
    this.terminal.open(this.mountPoint);
    this.viewport = $('.xterm-viewport', this.mountPoint);
    this.terminal.parser.registerOscHandler(52, () => true);
    this.terminal.onData(value => this.input(value));
    this.terminal.onResize(({cols,rows}) => {
      clearTimeout(this.sizeTimer);
      this.sizeTimer = setTimeout(() => {
        if (!this.meta.closed) void api(`${this.path}/size`,{cols,rows}).catch(() => {});
      }, 65);
    });
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(this.host);
  }

  resize() {
    if (!this.mountPoint.offsetWidth || !this.terminal) return;
    // Fit uses the unpadded mount width. Overlay scrollbars can report zero occupied pixels.
    const occupied = this.viewport ? this.viewport.offsetWidth - this.viewport.clientWidth : 0;
    const padding = `${Math.max(0, 16-occupied)}px`;
    if (this.terminal.element.style.paddingRight!==padding) this.terminal.element.style.paddingRight=padding;
    this.fit.fit();
  }

  applySize() {
    const size = this.workspace.sizes.get(this.meta.id) || {};
    for (const axis of ['width','height']) {
      const value = size[axis];
      if (Number.isFinite(value) && value > 0) this.node.style.setProperty(`--terminal-${axis}`, `${value}px`);
      else this.node.style.removeProperty(`--terminal-${axis}`);
    }
  }

  setSize(width, height) {
    const size = {...this.workspace.sizes.get(this.meta.id)};
    const container = this.node.parentElement;
    if (width !== undefined && container?.classList.contains('tiled')) {
      const available = container.clientWidth;
      size.width = Math.round(Math.max(Math.min(380, available), Math.min(width, available)));
    }
    if (height !== undefined) size.height = Math.round(Math.max(200, Math.min(height, 1400)));
    this.workspace.sizes.set(this.meta.id,size);
    this.applySize();
  }

  bindResizeHandle() {
    const handle = $('.resize-pane',this.node);
    const reset = () => {
      this.workspace.sizes.delete(this.meta.id);
      this.applySize();
      this.workspace.persist();
    };
    let drag = null;
    handle.onpointerdown = event => {
      if (event.button!==0 || !event.isPrimary) return;
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      drag = {id:event.pointerId,x:event.clientX,y:event.clientY,width:this.node.getBoundingClientRect().width,height:this.host.offsetHeight};
      this.node.classList.add('resizing');
    };
    handle.onpointermove = event => {
      if (drag?.id!==event.pointerId) return;
      const dx=event.clientX-drag.x,dy=event.clientY-drag.y;
      this.setSize(dx ? drag.width+dx : undefined, dy ? drag.height+dy : undefined);
    };
    const finish = event => {
      if (drag?.id!==event.pointerId) return;
      drag=null;
      this.node.classList.remove('resizing');
      this.workspace.persist();
    };
    handle.onpointerup = finish;
    handle.onpointercancel = finish;
    handle.onlostpointercapture = finish;
    handle.ondblclick = reset;
    handle.onkeydown = event => {
      const dimensions={ArrowLeft:[-40,0],ArrowRight:[40,0],ArrowUp:[0,-40],ArrowDown:[0,40]}[event.key];
      if(!dimensions && event.key!=='Home')return;
      event.preventDefault();
      if(event.key==='Home')return reset();
      const [dx,dy]=dimensions;
      this.setSize(dx?this.node.offsetWidth+dx:undefined,dy?this.host.offsetHeight+dy:undefined);
      this.workspace.persist();
    };
  }

  mount() {
    this.open();
    requestAnimationFrame(() => this.resize());
  }

  dispose() {
    clearTimeout(this.inputTimer);
    clearTimeout(this.sizeTimer);
    this.observer?.disconnect();
    this.terminal?.dispose();
    this.node.remove();
  }

  input(value) {
    if (this.meta.closed) return;
    this.pendingInput += value;
    if (!this.inputTimer) this.inputTimer = setTimeout(() => this.flushInput(), 10);
  }

  flushInput() {
    this.inputTimer = null;
    const chars = Array.from(this.pendingInput), gate = this.takeover;
    this.pendingInput = '';
    this.inputChain = this.inputChain.then(async () => {
      if (gate) await gate;
      for (let start=0; start<chars.length; start+=4000) {
        await api(`${this.path}/input`, {text:chars.slice(start,start+4000).join('')});
      }
      this.error('');
    }).catch(error => this.error(error.message));
  }

  control(action) {
    const promise = api(`${this.path}/control`,{action});
    if (action === 'takeover') this.takeover = promise;
    void promise.then(() => { this.error(''); this.terminal.focus(); }).catch(error => this.error(error.message))
      .finally(() => { if (this.takeover === promise) this.takeover = null; });
  }

  error(text) {
    const el = $('.terminal-error',this.node);
    el.textContent = text;
    el.hidden = !text;
  }

  accept(packet) {
    if (packet.reset) this.terminal.reset();
    if (packet.text) this.terminal.write(Uint8Array.from(atob(packet.text), char=>char.charCodeAt(0)));
    this.revision = packet.revision;
    if(this.reconnecting){this.reconnecting=false;this.error('');}
    this.render(packet);
  }

  render(packet) {
    const oldPhase = this.packet?.run?.phase;
    const oldHistory = this.packet?.history?.[0]?.id;
    const oldUser = this.packet?.context?.user;
    this.packet = packet;
    this.meta.closed = packet.closed;
    const context = packet.context;
    $('.term-name',this.node).textContent = packet.run?.name || this.meta.connectionName;
    $('.term-context',this.node).textContent = packet.closed ? 'SSH 已断开' : context
      ? `${packet.run ? this.meta.connectionName+' · ' : ''}${context.user} · ${context.directory}` : '正在连接。请在终端处理 SSH 提示。';
    const run = packet.run;
    $('.run-info',this.node).hidden = !run;
    if (run) {
      const statuses = {pending:'待执行',running:'执行中',succeeded:'通过',failed:'失败',manual:'手工完成','needs-review':'待核对'};
      $('.run-message',this.node).textContent = run.message;
      $('.run-steps',this.node).replaceChildren(...run.steps.map(step => {
        const item=document.createElement('li'); item.dataset.status=step.status;
        item.textContent=`${step.name} · ${statuses[step.status]}`; return item;
      }));
      $('[data-action="takeover"]',this.node).hidden = !activePhases.includes(run.phase) || run.takeover || run.phase === 'paused';
      for (const action of ['retry','next']) {
        const node = $(`[data-action="${action}"]`,this.node);
        const expected = action==='next' && run.switchTo ? run.switchTo : run.user;
        node.hidden = run.phase !== 'paused'; node.disabled = !packet.ready || context?.user !== expected;
      }
      $('[data-action="end"]',this.node).hidden = !activePhases.includes(run.phase);
    }
    if (packet.closed || oldPhase !== run?.phase) this.workspace.drawTabs();
    const newest = packet.history?.[0];
    if (this.workspace.active() === this) {
      const changedHistory=oldHistory!==newest?.id;
      if (changedHistory && newest?.switchTo && !run) state.side='history';
      if (oldUser!==context?.user || packet.closed || changedHistory && state.side==='history')renderSidebar();
    }
  }
}

class TerminalWorkspace {
  constructor() {
    this.panels = new Map();
    this.selected = null;
    this.tiled = true;
    this.visible = false;
    this.sizes = new Map();
    this.ordinals = new Map();
    this.nextOrdinal = 1;
    try {
      const saved = JSON.parse(sessionStorage.getItem('terminalLayout'));
      this.selected = saved?.selected || saved?.slots?.[saved.focused] || null;
      this.tiled = saved?.mode !== 'single';
      if(saved?.sizes) this.sizes=new Map(Object.entries(saved.sizes));
      if(saved?.ordinals) this.ordinals=new Map(Object.entries(saved.ordinals).filter(([,number])=>Number.isSafeInteger(number)&&number>0));
      this.nextOrdinal=Math.max(0,...this.ordinals.values())+1;
      this.dismissed = new Set(Array.isArray(saved?.dismissed) ? saved.dismissed : []);
    } catch (_) { this.dismissed = new Set(); }
  }

  async refresh() {
    const entries = await api('terminals');
    const ids = new Set(entries.map(entry=>entry.id));
    for (const [id,panel] of this.panels) if (!ids.has(id)) { panel.dispose(); this.panels.delete(id); }
    for (const entry of entries) {
      if (this.dismissed.has(entry.id)) continue;
      if (this.panels.has(entry.id)) this.panels.get(entry.id).meta=entry;
      else {
        const ordinal=this.ordinals.get(entry.id) || this.nextOrdinal++;
        this.ordinals.set(entry.id,ordinal);
        this.panels.set(entry.id,new TerminalPanel(entry,this,ordinal));
      }
    }
    this.dismissed=new Set([...this.dismissed].filter(id=>ids.has(id)));
    for(const id of this.sizes.keys())if(!ids.has(id)||this.dismissed.has(id))this.sizes.delete(id);
    for(const id of this.ordinals.keys())if(!ids.has(id)||this.dismissed.has(id))this.ordinals.delete(id);
    if (!this.panels.has(this.selected)) this.selected=this.panels.keys().next().value || null;
    this.layout();
  }

  active() { return this.panels.get(this.selected); }

  focus(id, withKeyboard=false) {
    if (!this.panels.has(id)) return;
    if (this.selected!==id) { this.selected=id; message('#sidebar-notice'); this.layout(); }
    if (withKeyboard) this.active()?.terminal?.focus();
  }

  toggleTiles() {
    this.tiled=!this.tiled;
    this.layout();
    this.active()?.terminal?.focus();
  }

  async close(id) {
    const panel=this.panels.get(id);
    if (!panel) return;
    try {
      await api(`${panel.path}/close`,{});
      this.dismissed.add(id); panel.dispose(); this.panels.delete(id);
      await this.refresh();
    } catch(error) { actionNotice(error.message); }
  }

  drawTabs() {
    const root=$('#terminal-tabs');
    root.hidden=this.panels.size===0;
    root.replaceChildren(...[...this.panels.values()].map(panel=>{
      const id=panel.meta.id, tab=document.createElement('span'); tab.className='terminal-tab';
      const label=button('',()=>this.focus(id,true));
      label.className='terminal-tab-label'; label.title=panel.meta.connectionName;
      const number=document.createElement('span'), name=document.createElement('span');
      number.className='terminal-index';number.textContent=String(panel.ordinal);
      name.className='terminal-tab-name';name.textContent=panel.meta.connectionName;
      label.append(number,name);label.setAttribute('aria-label',`终端 ${panel.ordinal} · ${panel.meta.connectionName}`);
      label.setAttribute('aria-pressed',String(this.active()===panel));
      if(panel.meta.closed) label.classList.add('offline');
      const close=button('×',()=>this.close(id)); close.className='tab-close'; close.setAttribute('aria-label',`关闭终端 ${panel.ordinal}`);
      tab.append(label,close); return tab;
    }));
  }

  watch(panels) {
    const visible=panels.filter(panel=>!panel.packet?.closed);
    const selection=visible.map(panel=>panel.meta.id).join(',');
    if (this.subscription?.selection===selection) return;
    this.subscription?.abort(); this.subscription=null;
    if (!visible.length) return;
    const controller=new AbortController(); controller.selection=selection;
    this.subscription=controller;
    void this.read(controller,visible);
  }

  async read(controller,panels) {
    while (!controller.signal.aborted) {
      const terminals=panels.filter(panel=>!panel.packet?.closed)
        .map(panel=>({id:panel.meta.id,after:panel.revision}));
      if(!terminals.length)break;
      try {
        const result=await api('terminal-events',{terminals},controller.signal);
        if(controller.signal.aborted)return;
        for(const packet of result.packets)this.panels.get(packet.id)?.accept(packet);
        if(result.missing.length){await this.refresh();break;}
      } catch(error) {
        if(controller.signal.aborted)return;
        if(error.status===401){showLogin();return;}
        panels.forEach(panel=>{panel.reconnecting=true;panel.error('正在重新连接终端显示…');});
        await new Promise(resolve=>setTimeout(resolve,850));
      }
    }
    if(this.subscription===controller)this.subscription=null;
  }

  layout() {
    const root=$('#terminal-main');
    const tiled=this.tiled && this.panels.size>1;
    const shown=!this.visible ? [] : tiled ? [...this.panels.values()] : [this.active()].filter(Boolean);
    const nodes=shown.map(panel=>panel.node);
    if(nodes.length!==root.children.length || nodes.some((node,i)=>node!==root.children[i]))root.replaceChildren(...nodes);
    root.classList.toggle('tiled',tiled);
    const splitter=$('#toggle-split'); splitter.disabled=this.panels.size<2;
    splitter.setAttribute('aria-pressed',String(tiled)); splitter.textContent=tiled?'单窗口':'平铺窗口';
    $('#no-terminal-windows').hidden=this.panels.size>0;
    for(const panel of shown) {
      const active=panel===this.active();panel.node.classList.toggle('focused',active);
      $('.focus-pane',panel.node).hidden=!tiled||active;
      $('.selected-pane',panel.node).hidden=!active;
      panel.mount();
    }
    this.watch(shown);
    this.drawTabs(); renderSidebar();
    this.persist();
  }

  persist() {
    sessionStorage.setItem('terminalLayout',JSON.stringify({selected:this.selected,mode:this.tiled?'grid':'single',
      dismissed:[...this.dismissed],sizes:Object.fromEntries(this.sizes),ordinals:Object.fromEntries(this.ordinals)}));
  }
}
