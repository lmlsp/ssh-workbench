'use strict';

class TerminalPanel {
  constructor(meta, workspace) {
    this.meta = meta;
    this.workspace = workspace;
    this.revision = -1;
    this.packet = null;
    this.pendingInput = '';
    this.inputChain = Promise.resolve();
    this.node = $('#terminal-template').content.firstElementChild.cloneNode(true);
    this.node.dataset.session = meta.id;
    this.host = $('.xterm-host', this.node);
    $('.term-name', this.node).textContent = meta.name;
    $('.focus-pane', this.node).onclick = () => this.workspace.focus(this.meta.id, true);
    const close = $('.close-pane', this.node);
    close.onclick = () => this.workspace.close(this.meta.id);
    close.setAttribute('aria-label', `关闭窗口 ${meta.name}`);
    this.host.addEventListener('focusin', () => this.workspace.focus(this.meta.id));
    $$('[data-action]', this.node).forEach(node => { node.onclick = () => this.control(node.dataset.action); });
  }

  get path() { return `terminal/${this.meta.id}`; }

  open() {
    if (this.terminal) return;
    this.terminal = new Terminal({ fontFamily:'Cascadia Mono, Noto Sans Mono CJK SC, monospace', fontSize:14, cursorBlink:true,
      theme:{background:'#132136', foreground:'#e4edf9', cursor:'#abceff', selectionBackground:'#345989'}, scrollback:4500 });
    this.fit = new FitAddon.FitAddon();
    this.terminal.loadAddon(this.fit);
    this.terminal.open(this.host);
    this.terminal.parser.registerOscHandler(52, () => true);
    this.terminal.onData(value => this.input(value));
    this.terminal.onResize(({cols,rows}) => {
      if (!this.meta.closed) void api(`${this.path}/size`,{cols,rows}).catch(() => {});
    });
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(this.host);
  }

  resize() { if (this.host.offsetWidth) this.fit?.fit(); }

  mount() {
    this.open();
    requestAnimationFrame(() => this.resize());
  }

  dispose() {
    clearTimeout(this.inputTimer);
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
    this.packet = packet;
    this.meta.closed = packet.closed;
    const context = packet.context;
    $('.term-name',this.node).textContent = packet.run?.name || this.meta.connectionName;
    $('.term-context',this.node).textContent = packet.closed ? 'SSH 已断开' : context
      ? `${this.meta.connectionName} · ${context.user} · ${context.directory}` : '正在连接。请在终端处理 SSH 提示。';
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
    if (this.workspace.active() === this && oldHistory!==newest?.id) {
      if (newest?.switchTo && !run) state.side='history';
      if (state.side==='history') renderSidebar();
    }
  }
}

class TerminalWorkspace {
  constructor() {
    this.panels = new Map();
    this.selected = null;
    this.tiled = true;
    this.visible = false;
    try {
      const saved = JSON.parse(sessionStorage.getItem('terminalLayout'));
      this.selected = saved?.selected || saved?.slots?.[saved.focused] || null;
      this.tiled = saved?.mode !== 'single';
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
      else this.panels.set(entry.id,new TerminalPanel(entry,this));
    }
    this.dismissed=new Set([...this.dismissed].filter(id=>ids.has(id)));
    if (!this.panels.has(this.selected)) this.selected=this.panels.keys().next().value || null;
    this.layout();
  }

  active() { return this.panels.get(this.selected); }

  focus(id, withKeyboard=false) {
    if (!this.panels.has(id)) return;
    if (this.selected!==id) { this.selected=id; this.layout(); }
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
    } catch(error) { message('#notice',error.message); }
  }

  drawTabs() {
    const root=$('#terminal-tabs');
    root.hidden=this.panels.size===0;
    root.replaceChildren(...[...this.panels.values()].map(panel=>{
      const id=panel.meta.id, tab=document.createElement('span'); tab.className='terminal-tab';
      const label=button(panel.meta.name,()=>this.focus(id,true));
      label.className='terminal-tab-label'; label.title=panel.meta.connectionName;
      label.setAttribute('aria-pressed',String(this.active()===panel));
      if(panel.meta.closed) label.classList.add('offline');
      const close=button('×',()=>this.close(id)); close.className='tab-close'; close.setAttribute('aria-label',`关闭 ${panel.meta.name}`);
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
      panel.mount();
    }
    this.watch(shown);
    this.drawTabs(); renderSidebar();
    sessionStorage.setItem('terminalLayout',JSON.stringify({selected:this.selected,mode:this.tiled?'grid':'single',dismissed:[...this.dismissed]}));
  }
}
