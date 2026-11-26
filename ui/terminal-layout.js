'use strict';

const terminalSplitPresets = ['auto', 'columns', 'rows'];
const terminalPaneMinimum = { width: 244, height: 194 };
const terminalDividerSize = 8;

function parseTerminalTree(node, seen = new Set(), depth = 0) {
  if (typeof node === 'string' && /^[\w-]{20,28}$/.test(node) && !seen.has(node) && seen.size < 12) {
    seen.add(node);
    return node;
  }
  if (!node || typeof node!=='object' || depth>=12 || !['columns','rows'].includes(node.axis)) return null;
  const first=parseTerminalTree(node.first,seen,depth+1);
  const second=parseTerminalTree(node.second,seen,depth+1);
  if (!first || !second) return first || second;
  const ratio=Number.isFinite(node.ratio)?Math.max(.01,Math.min(.99,node.ratio)):.5;
  return {axis:node.axis,ratio,first,second};
}

function terminalLeaves(node) {
  if (!node) return [];
  return typeof node==='string'?[node]:[...terminalLeaves(node.first),...terminalLeaves(node.second)];
}

function keepTerminalLeaves(node, allowed) {
  if (!node || typeof node==='string') return allowed.has(node)?node:null;
  const first=keepTerminalLeaves(node.first,allowed), second=keepTerminalLeaves(node.second,allowed);
  if (!first || !second) return first||second;
  return first===node.first && second===node.second?node:{...node,first,second};
}

function replaceTerminalLeaf(node, id, replacement) {
  if (typeof node==='string')return node===id?replacement:node;
  const first=replaceTerminalLeaf(node.first,id,replacement),second=replaceTerminalLeaf(node.second,id,replacement);
  return first===node.first && second===node.second?node:{...node,first,second};
}

function splitTerminalLeaves(ids, axis) {
  if (ids.length<2)return ids[0]||null;
  const middle=Math.ceil(ids.length/2);
  return {axis,ratio:middle/ids.length,
    first:splitTerminalLeaves(ids.slice(0,middle),axis),
    second:splitTerminalLeaves(ids.slice(middle),axis)};
}

function terminalTreeMinimum(axis, first, second) {
  return axis==='columns'
    ?{width:first.width+second.width+terminalDividerSize,height:Math.max(first.height,second.height)}
    :{width:Math.max(first.width,second.width),height:first.height+second.height+terminalDividerSize};
}

class TerminalLayout {
  constructor(workspace) {
    this.workspace=workspace;
    this.preset='auto';
  }

  restore(saved) {
    this.tree=parseTerminalTree(saved?.tree);
    this.preset=this.tree?'custom':terminalSplitPresets.includes(saved?.preset)?saved.preset:'auto';
  }

  bind(root) {
    if (this.root) return;
    this.root=root;
    this.widthObserver=new ResizeObserver(([entry])=>{
      if (!entry.contentRect.width) return;
      const wide=entry.contentRect.width>=terminalPaneMinimum.width*2+terminalDividerSize;
      if (this.wide!==wide) {
        this.wide=wide;
        this.workspace.layout();
      }
    });
    this.widthObserver.observe(root.parentElement);
    root.addEventListener('dragover',event=>this.dragOver(event));
    root.addEventListener('dragleave',event=>{
      if (!(event.relatedTarget instanceof Node) || !root.contains(event.relatedTarget)) this.markDrop();
    });
    root.addEventListener('drop',event=>this.drop(event));
  }

  setPreset(preset) {
    this.preset=preset;
    this.key=null;
  }

  reconcile(ids, selected) {
    const previous=this.tree;
    if (this.preset==='custom') {
      let tree=keepTerminalLeaves(previous,new Set(ids));
      const leaves=new Set(terminalLeaves(tree));
      for (const id of ids) {
        if (leaves.has(id))continue;
        const target=leaves.has(selected)?selected:[...leaves].at(-1);
        tree=target?replaceTerminalLeaf(tree,target,{axis:'rows',ratio:.5,first:target,second:id}):id;
        leaves.add(id);
      }
      this.tree=tree;
      if (ids.length<2)this.setPreset('auto');
      return;
    }
    const key=JSON.stringify([this.preset,this.wide,ids]);
    if (key===this.key)return;
    this.key=key;
    if (this.preset==='auto' && this.wide!==false && ids.length>1) {
      const middle=Math.ceil(ids.length/2);
      this.tree={axis:'columns',ratio:.5,
        first:splitTerminalLeaves(ids.slice(0,middle),'rows'),
        second:splitTerminalLeaves(ids.slice(middle),'rows')};
    } else this.tree=splitTerminalLeaves(ids,this.preset==='columns'?'columns':'rows');
  }

  show(node, tiled) {
    const root=this.root;
    root.classList.toggle('tiled',tiled);
    if (node===this.rendered)return;
    this.finishResize?.();
    this.rendered=node;
    const view=node?this.createView(node):null;
    root.style.setProperty('--layout-min-width', tiled&&view?`${view.width}px`:'0px');
    root.style.setProperty('--layout-min-height', view?`${view.height}px`:'0px');
    root.replaceChildren(...(view?[view.element]:[]));
  }

  createView(node) {
    if (typeof node==='string')return {element:this.workspace.panels.get(node).node,...terminalPaneMinimum};
    const first=this.createView(node.first),second=this.createView(node.second);
    const element=document.createElement('div');
    element.className='terminal-split '+node.axis;
    const divider=document.createElement('div');
    divider.className='terminal-divider';
    divider.tabIndex=0;
    divider.setAttribute('role','separator');
    divider.setAttribute('aria-orientation',node.axis==='columns'?'vertical':'horizontal');
    divider.setAttribute('aria-label',node.axis==='columns'?'调整左右终端宽度':'调整上下终端高度');
    divider.setAttribute('aria-valuemin','0');
    divider.setAttribute('aria-valuemax','100');
    divider.title='拖动或用方向键调整，双击均分';
    element.append(first.element,divider,second.element);
    this.bindDivider(node,element,divider,first,second);
    return {element,...terminalTreeMinimum(node.axis,first,second)};
  }

  bindDivider(split, element, divider, first, second) {
    const columns=split.axis==='columns';
    const dimension=columns?'width':'height';
    const coordinate=columns?'clientX':'clientY';
    const offset=columns?'offsetWidth':'offsetHeight';
    const apply=()=>{
      element.style.setProperty('--first-share',split.ratio+'fr');
      element.style.setProperty('--second-share',(1-split.ratio)+'fr');
      divider.setAttribute('aria-valuenow',String(Math.round(split.ratio*100)));
    };
    element.style.setProperty('--first-min',first[dimension]+'px');
    element.style.setProperty('--second-min',second[dimension]+'px');
    apply();
    const total=()=>element[offset]-terminalDividerSize;
    const change=(pixels, available)=>{
      if (available<=0)return;
      const position=Math.max(first[dimension],Math.min(pixels,available-second[dimension]));
      const ratio=position/available;
      if (ratio===split.ratio)return;
      split.ratio=ratio;
      this.preset='custom';
      apply();
    };
    let drag=null, frame=0;
    const reposition=()=>{
      frame=0;
      if (drag)change(drag.start+drag.delta,drag.available);
    };
    divider.onpointerdown=event=>{
      if (event.button!==0 || !event.isPrimary)return;
      divider.setPointerCapture(event.pointerId);
      drag={id:event.pointerId,origin:event[coordinate],delta:0,start:first.element[offset],available:total()};
      this.root.classList.add('resizing-'+split.axis);
      this.workspace.beginSplitResize();
      this.finishResize=finish;
    };
    divider.onpointermove=event=>{
      if (drag?.id!==event.pointerId)return;
      drag.delta=event[coordinate]-drag.origin;
      if (!frame)frame=requestAnimationFrame(reposition);
    };
    const finish=event=>{
      if (!drag || (event && drag.id!==event.pointerId))return;
      if (frame)cancelAnimationFrame(frame);
      if (drag.delta)reposition();
      frame=0;
      const id=drag.id;
      drag=null;
      this.finishResize=null;
      if (divider.hasPointerCapture(id))divider.releasePointerCapture(id);
      this.root.classList.remove('resizing-'+split.axis);
      this.workspace.endSplitResize();
    };
    divider.onpointerup=finish;
    divider.onpointercancel=finish;
    divider.onlostpointercapture=finish;
    const equalize=()=>{
      const available=total();
      change(available/2,available);
      this.workspace.layoutChanged();
    };
    divider.ondblclick=equalize;
    divider.onkeydown=event=>{
      const negative=columns?'ArrowLeft':'ArrowUp', positive=columns?'ArrowRight':'ArrowDown';
      if (![negative,positive,'Home'].includes(event.key))return;
      event.preventDefault();
      if (event.key==='Home')return equalize();
      const amount=(event.key===negative?-1:1)*(event.shiftKey?80:24);
      change(first.element[offset]+amount,total());
      this.workspace.layoutChanged();
    };
  }

  startDrag(event,id) {
    if (this.workspace.panels.size<2) {
      event.preventDefault();
      return;
    }
    this.dragged=id;
    event.dataTransfer.setData('application/x-workbench-terminal',id);
    event.dataTransfer.effectAllowed='move';
  }

  endDrag() {
    this.dragged=null;
    this.markDrop();
  }

  markDrop(panel, edge) {
    if (this.dropTarget!==panel) this.dropTarget?.removeAttribute('data-drop-edge');
    this.dropTarget=panel;
    if (panel)panel.dataset.dropEdge=edge;
  }

  dragOver(event) {
    const panel=event.target.closest('.terminal-card');
    if (!this.dragged || !panel || panel.dataset.session===this.dragged) {
      this.markDrop();
      return;
    }
    const {left,top,width,height}=panel.getBoundingClientRect();
    const x=(event.clientX-left)/width,y=(event.clientY-top)/height;
    const distance={left:x,right:1-x,top:y,bottom:1-y};
    const edge=Object.keys(distance).reduce((nearest,key)=>distance[key]<distance[nearest]?key:nearest);
    if (distance[edge]>.27) {
      this.markDrop();
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect='move';
    this.markDrop(panel,edge);
  }

  drop(event) {
    const target=this.dropTarget,id=this.dragged;
    const edge=target?.dataset.dropEdge;
    const neighbor=target?.dataset.session;
    this.endDrag();
    if (!edge || !id || !this.workspace.panels.has(id) || !this.workspace.panels.has(neighbor))return;
    event.preventDefault();
    const before=edge==='left'||edge==='top';
    const axis=edge==='left'||edge==='right'?'columns':'rows';
    const tree=keepTerminalLeaves(this.tree,new Set([...this.workspace.panels.keys()].filter(key=>key!==id)));
    this.tree=replaceTerminalLeaf(tree,neighbor,{axis,ratio:.5,first:before?id:neighbor,second:before?neighbor:id});
    this.preset='custom';
    this.workspace.tiled=true;
    this.workspace.focus(id,true,true);
  }
}
