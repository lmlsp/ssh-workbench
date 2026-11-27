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
    $('.term-name', this.node).textContent = this.connectionName;
    $('.focus-pane', this.node).onclick = () => this.workspace.focus(this.meta.id, true);
    const heading=$('.terminal-heading',this.node);
    heading.addEventListener('click',event=>{
      if (!event.target.closest('button') && !getSelection()?.toString()) this.workspace.focus(this.meta.id,true);
    });
    $('.maximize-pane',heading).onclick=()=>this.workspace.toggleTiles(this.meta.id);
    const close = $('.close-pane', this.node);
    close.onclick = () => this.workspace.close(this.meta.id);
    close.setAttribute('aria-label', `关闭终端 ${ordinal} · ${meta.connectionName}`);
    this.host.addEventListener('focusin', () => this.workspace.focus(this.meta.id));
    $$('[data-action]',this.node).forEach(node=>{
      node.onclick=()=>this.control(node.dataset.action==='pause' && (this.packet?.run?.takeover || this.packet?.run?.beforeStep)
        ? 'resume' : node.dataset.action);
    });
    this.applyAppearance();
  }

  get path() { return `terminal/${this.meta.id}`; }
  get connectionId() { return this.packet?.endpoint?.id || this.meta.connection; }
  get connectionName() { return byId('connections',this.connectionId)?.name || this.packet?.endpoint?.name || this.meta.connectionName; }

  open() {
    if (this.terminal) return;
    this.terminal = new Terminal({ fontFamily:'Cascadia Mono, Noto Sans Mono CJK SC, monospace', fontSize:14, cursorBlink:true,
      theme:this.workspace.appearance?.colors||{background:'#111c2c', foreground:'#e4edf9', cursor:'#abceff', selectionBackground:'#345989'}, scrollback:4500 });
    this.fit = new FitAddon.FitAddon();
    this.terminal.loadAddon(this.fit);
    this.terminal.open(this.mountPoint);
    this.viewport = $('.xterm-viewport', this.mountPoint);
    this.terminal.parser.registerOscHandler(52, () => true);
    this.terminal.onData(value => this.input(value));
    this.terminal.onResize(dimensions=>{
      this.pendingDimensions=dimensions;
      this.scheduleSize();
    });
    this.observer = new ResizeObserver(() => this.scheduleFit());
    this.observer.observe(this.host);
  }

  resize() {
    if (!this.mountPoint.offsetWidth || !this.mountPoint.offsetHeight || !this.terminal) return;
    // Fit uses the unpadded mount width. Overlay scrollbars can report zero occupied pixels.
    const occupied = this.viewport ? this.viewport.offsetWidth - this.viewport.clientWidth : 0;
    const padding = `${Math.max(0, 16-occupied)}px`;
    if (this.terminal.element.style.paddingRight!==padding) this.terminal.element.style.paddingRight=padding;
    this.fit.fit();
  }

  applyAppearance() {
    const appearance=this.workspace.appearance;
    if(!appearance)return;
    for(const [name,value] of Object.entries(appearance.variables))this.node.style.setProperty(name,value);
    if(this.terminal)this.terminal.options.theme=appearance.colors;
  }

  scheduleFit() {
    if (this.fitFrame)return;
    this.fitFrame=requestAnimationFrame(()=>{
      this.fitFrame=0;
      this.resize();
    });
  }

  scheduleSize() {
    clearTimeout(this.sizeTimer);
    if (!this.pendingDimensions || this.workspace.splitResizing)return;
    this.sizeTimer=setTimeout(()=>{
      if (this.workspace.splitResizing || this.meta.closed)return;
      const size=this.pendingDimensions;
      this.pendingDimensions=null;
      void api(`${this.path}/size`,size).catch(()=>{});
    },65);
  }

  mount() {
    this.open();
    this.scheduleFit();
  }

  dispose() {
    clearTimeout(this.inputTimer);
    clearTimeout(this.sizeTimer);
    cancelAnimationFrame(this.fitFrame);
    clearTimeout(this.completionTimer);
    clearInterval(this.clockTimer);
    this.observer?.disconnect();
    this.terminal?.dispose();
    this.node.remove();
    this.tab?.remove();
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
    if(action==='resume' && this.inputTimer){
      clearTimeout(this.inputTimer);
      this.flushInput();
    }
    const send=()=>api(`${this.path}/control`,{action});
    const promise=action==='resume'?this.inputChain.then(send):send();
    if (action === 'pause') this.takeover = promise;
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

  renderClock(run) {
    clearInterval(this.clockTimer);
    const clock=$('.run-clock',this.node), waiting=run?.phase==='waiting';
    clock.hidden=!waiting && !(run?.phase==='paused' && run.beforeStep && run.waitMs>0);
    if(clock.hidden)return;
    const next=run.steps[run.index]?.name, deadline=performance.now()+run.waitMs;
    const tick=()=>{
      const remaining=Math.max(0,Math.ceil((waiting?deadline-performance.now():run.waitMs)/1000));
      clock.textContent=waiting?`${remaining} 秒后执行「${next}」`:`倒计时暂停 · 剩余 ${remaining} 秒`;
    };
    tick();
    if(waiting)this.clockTimer=setInterval(tick,200);
  }

  render(packet) {
    const oldPhase = this.packet?.run?.phase;
    const oldRunId = this.packet?.run?.id;
    const oldHistory = this.packet?.history?.[0]?.id;
    const oldUser = this.packet?.context?.user;
    const oldConnection = this.connectionId;
    this.packet = packet;
    this.meta.closed = packet.closed;
    const context = packet.context;
    const changedConnection = oldConnection !== this.connectionId;
    $('.term-name',this.node).textContent = activePhases.includes(packet.run?.phase) && packet.run.connection===this.connectionId
      ? packet.run.name : this.connectionName;
    const contextLabel=$('.term-context',this.node);
    contextLabel.textContent = packet.closed ? 'SSH 已断开' : context
      ? `${context.user} · ${context.directory}` : '正在连接。请在终端处理 SSH 提示。';
    contextLabel.title=contextLabel.textContent;
    const inputNotice = $('.terminal-input-notice',this.node);
    inputNotice.textContent = packet.inputNotice || '';
    inputNotice.hidden = !packet.inputNotice;
    const run = packet.run;
    const summary=$('.run-info',this.node);
    const complete=run?.phase==='succeeded';
    if(!complete){clearTimeout(this.completionTimer);this.completionExpired=false;}
    if(complete && (oldPhase!=='succeeded'||oldRunId!==run.id)) {
      clearTimeout(this.completionTimer);
      const remaining=Math.max(0,5000-(run.completionAgeMs??0));
      this.completionExpired=remaining===0;
      if(remaining)this.completionTimer=setTimeout(()=>{
        this.completionExpired=true;summary.hidden=true;
      },remaining);
    }
    summary.classList.toggle('run-complete',complete);
    summary.hidden=!run || run.phase==='exited' || complete && this.completionExpired;
    this.renderClock(run);
    if (run) {
      $('.run-message',this.node).textContent = run.message;
      $('.run-steps',this.node).replaceChildren(...run.steps.map(step => {
        const item=document.createElement('li'); item.dataset.status=step.status;
        item.textContent=`${step.name} · ${runStatus[step.status]}`; return item;
      }));
      const toggle=$('[data-action="pause"]',this.node), recover=run.phase==='paused'&&!run.beforeStep;
      const resuming=run.takeover||run.beforeStep;
      toggle.hidden=!activePhases.includes(run.phase)||run.phase==='stopping'||recover;
      toggle.textContent=resuming?'继续':'暂停';
      toggle.disabled=run.phase==='paused'&&(!packet.ready||context?.user!==run.user||run.connection!==this.connectionId);
      for (const action of ['retry','next']) {
        const node = $(`[data-action="${action}"]`,this.node);
        const expected = action==='next' && run.switchTo ? run.switchTo : run.user;
        node.hidden = !recover; node.disabled = !packet.ready || context?.user !== expected || run.connection!==this.connectionId;
      }
      $('[data-action="leave"]',this.node).hidden = !activePhases.includes(run.phase) || run.phase==='stopping';
    }
    if (packet.closed || oldPhase !== run?.phase || changedConnection) this.workspace.drawTabs();
    if ($('#parameters').open && state.parameters?.data.terminal===packet.id) updateRunAvailability();
    const newest = packet.history?.[0];
    if (this.workspace.active() === this) {
      publishWorkspaceState();
      const changedHistory=oldHistory!==newest?.id;
      if (changedHistory && newest?.switchTo && !run) state.side='history';
      if (oldUser!==context?.user || packet.closed || changedConnection || changedHistory && state.side==='history')renderSidebar();
    }
  }
}

class TerminalWorkspace {
  constructor() {
    this.panels = new Map();
    this.selected = null;
    this.tiled = true;
    this.visible = false;
    this.splitLayout=new TerminalLayout(this);
    this.ordinals = new Map();
    this.refreshSequence = 0;
    this.lastAppliedRefresh = 0;
    try {
      const saved = JSON.parse(sessionStorage.getItem('terminalLayout'));
      this.selected = saved?.selected || saved?.slots?.[saved.focused] || null;
      this.tiled = saved?.mode !== 'single';
      this.splitLayout.restore(saved);
      if(saved?.ordinals) this.ordinals=new Map(Object.entries(saved.ordinals).filter(([,number])=>Number.isSafeInteger(number)&&number>0));
      this.dismissed = new Set(Array.isArray(saved?.dismissed) ? saved.dismissed : []);
    } catch (_) { this.dismissed = new Set(); }
  }

  async refresh(discardClosed=false) {
    const sequence = ++this.refreshSequence;
    const entries = await api('terminals');
    if(sequence < this.lastAppliedRefresh)return false;
    this.lastAppliedRefresh = sequence;
    const ids = new Set(entries.map(entry=>entry.id));
    if(discardClosed)for(const entry of entries)if(entry.closed)this.dismissed.add(entry.id);
    for (const [id,panel] of this.panels) if (!ids.has(id)||this.dismissed.has(id)) { panel.dispose(); this.panels.delete(id); }
    this.dismissed=new Set([...this.dismissed].filter(id=>ids.has(id)));
    for(const id of this.ordinals.keys())if(!ids.has(id)||this.dismissed.has(id))this.ordinals.delete(id);
    const occupied=new Set(this.ordinals.values());
    let available=1;
    for (const entry of entries) {
      if (this.dismissed.has(entry.id)) continue;
      if (this.panels.has(entry.id)) this.panels.get(entry.id).meta=entry;
      else {
        let ordinal=this.ordinals.get(entry.id);
        if(!ordinal){
          while(occupied.has(available))available++;
          ordinal=available;
          occupied.add(ordinal);
        }
        this.ordinals.set(entry.id,ordinal);
        this.panels.set(entry.id,new TerminalPanel(entry,this,ordinal));
      }
    }
    if (!this.panels.has(this.selected)) this.selected=this.panels.keys().next().value || null;
    this.layout();
    return true;
  }

  active() { return this.panels.get(this.selected); }

  setAppearance(colors,variables) {
    this.appearance={colors,variables};
    for(const panel of this.panels.values())panel.applyAppearance();
  }

  focus(id, withKeyboard=false, updateLayout=false) {
    if (!this.panels.has(id)) return;
    if (this.selected!==id || updateLayout) { this.selected=id; message('#sidebar-notice'); this.layout(); }
    if (withKeyboard) {
      const panel=this.active();
      if (panel.node.isConnected) panel.node.scrollIntoView({block:'nearest',inline:'nearest'});
      panel.terminal?.focus();
    }
  }

  toggleTiles(id=this.selected) {
    if (this.panels.size<2)return;
    this.tiled=!this.tiled;
    this.focus(id,true,true);
  }

  setSplitLayout(preset) {
    if (preset!=='single' && !terminalSplitPresets.includes(preset))return;
    this.tiled=preset!=='single';
    if (this.tiled)this.splitLayout.setPreset(preset);
    this.focus(this.selected,true,true);
  }

  beginSplitResize() {
    this.splitResizing=true;
  }

  endSplitResize() {
    this.splitResizing=false;
    for(const panel of this.panels.values())panel.scheduleSize();
    this.layoutChanged();
  }

  layoutChanged() {
    publishWorkspaceState();
    this.persist();
  }

  async close(id) {
    const panel=this.panels.get(id);
    if (!panel) return;
    try {
      const job=panel.packet?.fileTransfer;
      if(job) {
        const confirmed=confirmTransferClose
          ? await confirmTransferClose(panel.connectionName)
          : window.confirm('中断文件传输并关闭终端？');
        if(!confirmed)return;
        await api(`files/${job}/cancel`,{});
        for(let attempt=0;attempt<30;attempt++) {
          if((await api(`files/${job}/status`)).released)break;
          await new Promise(resolve=>setTimeout(resolve,150));
        }
      }
      await api(`${panel.path}/close`,{});
      this.dismissed.add(id); panel.dispose(); this.panels.delete(id);
      await this.refresh();
    } catch(error) { actionNotice(error.message); }
  }

  drawTabs() {
    const root=$('#terminal-tabs');
    root.hidden=this.panels.size===0;
    for (const panel of this.panels.values()) {
      if (!panel.tab) {
        const id=panel.meta.id, tab=document.createElement('span');
        tab.className='terminal-tab';
        const label=button('',()=>this.focus(id,true));
        label.className='terminal-tab-label';
        label.draggable=true;
        label.ondragstart=event=>this.splitLayout.startDrag(event,id);
        label.ondragend=()=>this.splitLayout.endDrag();
        const number=document.createElement('span'), name=document.createElement('span');
        number.className='terminal-index'; number.textContent=String(panel.ordinal);
        name.className='terminal-tab-name';
        label.append(number,name);
        const close=button('×',()=>this.close(id));
        close.className='tab-close';
        close.setAttribute('aria-label',`关闭终端 ${panel.ordinal}`);
        tab.append(label,close);
        panel.tab=tab; panel.tabLabel=label; panel.tabName=name;
        root.append(tab);
      }
      const title=panel.connectionName;
      if (panel.tabName.textContent!==title)panel.tabName.textContent=title;
      panel.tabLabel.title=title;
      panel.tabLabel.setAttribute('aria-label',`终端 ${panel.ordinal} · ${title}`);
      panel.tabLabel.setAttribute('aria-pressed',String(this.active()===panel));
      panel.tabLabel.classList.toggle('offline',panel.meta.closed);
    }
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
    this.splitLayout.bind(root);
    this.splitLayout.reconcile([...this.panels.keys()],this.selected);
    const tiled=this.tiled && this.panels.size>1;
    const activePanel=this.active();
    const hadTerminalFocus=activePanel?.host.contains(document.activeElement);
    const shown=!this.visible ? [] : tiled ? [...this.panels.values()] : [activePanel].filter(Boolean);
    this.splitLayout.show(!shown.length?null:tiled?this.splitLayout.tree:this.selected,tiled);
    const toggle=$('#toggle-split');
    toggle.disabled=this.panels.size<2;
    toggle.setAttribute('aria-pressed',String(tiled));
    toggle.textContent=tiled?'单窗口':'分屏';
    $('#no-terminal-windows').hidden=this.panels.size>0;
    for(const panel of shown) {
      const active=panel===activePanel; panel.node.classList.toggle('focused',active);
      $('.focus-pane',panel.node).hidden=!tiled||active;
      $('.selected-pane',panel.node).hidden=!active;
      const maximize=$('.maximize-pane',panel.node);
      maximize.hidden=this.panels.size<2;
      maximize.textContent=tiled?'□':'❐';
      maximize.title=maximize.ariaLabel=tiled?'最大化此终端':'还原分屏';
      panel.mount();
    }
    if (hadTerminalFocus && activePanel.node.isConnected && !activePanel.host.contains(document.activeElement)) {
      activePanel.terminal?.focus();
    }
    this.watch(this.visible?[...this.panels.values()].filter(panel=>panel.terminal):[]);
    this.drawTabs(); renderSidebar();
    this.persist();
    // Selection changes also change the active file-browser context. Notify
    // Vue even when the newly focused terminal has not emitted a PTY event.
    publishWorkspaceState();
  }

  persist() {
    const {preset,tree}=this.splitLayout;
    sessionStorage.setItem('terminalLayout',JSON.stringify({selected:this.selected,mode:this.tiled?'split':'single',
      preset,tree:preset==='custom'?tree:undefined,
      dismissed:[...this.dismissed],ordinals:Object.fromEntries(this.ordinals)}));
  }
}
