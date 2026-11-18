'use strict';

class TerminalPanel {
  constructor(meta, workspace) {
    this.meta = meta;
    this.workspace = workspace;
    this.revision = 0;
    this.packet = null;
    this.pendingInput = '';
    this.inputChain = Promise.resolve();
    this.node = $('#terminal-template').content.firstElementChild.cloneNode(true);
    this.node.dataset.session = meta.id;
    this.host = $('.xterm-host', this.node);
    $('.term-name', this.node).textContent = meta.name;
    $('.focus-pane', this.node).onclick = () => this.workspace.focus(this.meta.id, true);
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
    if (!this.subscription && !this.packet?.closed) {
      this.subscription = new AbortController();
      void this.read(this.subscription);
    }
  }

  unmount() { this.subscription?.abort(); this.subscription = null; }

  dispose() {
    this.unmount();
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

  async read(subscription) {
    while (!subscription.signal.aborted) {
      try {
        const packet = await api(`${this.path}/events?after=${this.revision}&wait=1`, undefined, subscription.signal);
        if (subscription.signal.aborted) return;
        if (packet.reset) this.terminal.reset();
        if (packet.text) this.terminal.write(Uint8Array.from(atob(packet.text), char=>char.charCodeAt(0)));
        this.revision = packet.revision;
        this.render(packet);
        if (packet.closed) break;
      } catch (error) {
        if (subscription.signal.aborted) return;
        if (error.status === 401) { showLogin(); return; }
        this.error(error.status === 400 ? error.message : '正在重新连接终端显示…');
        if (error.status === 400) { void this.workspace.refresh(); break; }
        await new Promise(resolve => setTimeout(resolve, 850));
      }
    }
    if (this.subscription === subscription) this.subscription = null;
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
        node.hidden = run.phase !== 'paused'; node.disabled = !packet.ready || context?.user !== run.user;
      }
      $('[data-action="end"]',this.node).hidden = !activePhases.includes(run.phase);
    }
    if (packet.closed || oldPhase !== run?.phase) this.workspace.drawTabs();
    if (this.workspace.active() === this && state.side==='history' && oldHistory!==packet.history?.[0]?.id) renderSidebar();
    this.resize();
  }
}

class TerminalWorkspace {
  constructor() {
    this.panels = new Map();
    this.slots = [null,null];
    this.focused = 0;
    this.split = false;
    this.visible = false;
    try {
      const saved = JSON.parse(sessionStorage.getItem('terminalLayout'));
      if (Array.isArray(saved?.slots)) this.slots = [saved.slots[0] || null, saved.slots[1] || null];
      this.split = Boolean(saved?.split);
      this.focused = this.split && saved?.focused === 1 ? 1 : 0;
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
    this.slots=this.slots.map(id=>this.panels.has(id)?id:null);
    if (!this.slots[0]) this.slots[0]=this.slots[1] || this.panels.keys().next().value || null;
    if (this.slots[1]===this.slots[0]) this.slots[1]=null;
    if (this.split && !this.slots[1]) this.slots[1]=[...this.panels.keys()].find(id=>id!==this.slots[0]) || null;
    if (!this.slots[this.focused]) this.focused=0;
    this.layout();
  }

  active() { return this.panels.get(this.slots[this.focused]); }

  focus(id, withKeyboard=false) {
    const slot=this.slots.indexOf(id);
    if (slot < 0 || (!this.split && slot===1)) return;
    if (this.focused!==slot) { this.focused=slot; this.layout(); }
    if (withKeyboard) this.active()?.terminal?.focus();
  }

  choose(id, fresh=false) {
    if (!this.panels.has(id)) return;
    const displayed=this.slots.indexOf(id);
    if (displayed!==-1 && (displayed===0 || this.split)) return this.focus(id,true);
    if (this.split && fresh && !this.slots[1]) this.focused=1;
    this.slots[this.focused]=id;
    if (!this.split && this.slots[1]===id) this.slots[1]=null;
    this.layout();
    this.active()?.terminal?.focus();
  }

  toggleSplit() {
    this.split=!this.split;
    if (this.split) this.slots[1]=[...this.panels.keys()].find(id=>id!==this.slots[0]) || null;
    else if (this.focused===1 && this.slots[1]) this.slots[0]=this.slots[1];
    this.focused=0;
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
      const label=button(panel.meta.name,()=>this.choose(id));
      label.className='terminal-tab-label'; label.title=panel.meta.connectionName;
      label.setAttribute('aria-pressed',String(this.active()===panel));
      if(panel.meta.closed) label.classList.add('offline');
      const close=button('×',()=>this.close(id)); close.className='tab-close'; close.setAttribute('aria-label',`关闭 ${panel.meta.name}`);
      tab.append(label,close); return tab;
    }));
  }

  layout() {
    const actual=this.visible ? this.slots.slice(0,this.split?2:1) : [];
    for (const [id,panel] of this.panels) if (!actual.includes(id)) panel.unmount();
    $('#terminal-main').classList.toggle('split',this.split);
    const splitter=$('#toggle-split'); splitter.disabled=this.panels.size===0;
    splitter.setAttribute('aria-pressed',String(this.split)); splitter.textContent=this.split?'退出分屏':'左右分屏';
    $$('.terminal-slot').forEach((slot,index)=>{
      slot.hidden=index===1&&!this.split;
      if (slot.hidden) return;
      const panel=this.panels.get(actual[index]);
      slot.classList.toggle('focused',this.focused===index&&Boolean(panel));
      if (panel) {
        if (slot.firstChild!==panel.node) slot.replaceChildren(panel.node);
        $('.focus-pane',panel.node).hidden=!this.split||this.focused===index;
        panel.mount();
      } else {
        const empty=document.createElement('p'); empty.className='terminal-placeholder';
        empty.textContent=index===0?'新建终端后，在这里显示 SSH 会话。':'新建终端或选择上方其他终端标签。';
        const focus=button('在右侧打开',()=>{this.focused=1; this.layout(); $('#terminal-connection').focus();});
        focus.hidden=index===0||this.focused===1; slot.replaceChildren(empty,focus);
      }
    });
    this.drawTabs(); renderSidebar();
    sessionStorage.setItem('terminalLayout',JSON.stringify({slots:this.slots,split:this.split,focused:this.focused,dismissed:[...this.dismissed]}));
  }
}
