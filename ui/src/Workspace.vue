<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watchEffect } from 'vue';
import Button from 'primevue/button';
import Tag from 'primevue/tag';
import Splitter from 'primevue/splitter';
import SplitterPanel from 'primevue/splitterpanel';
import Tabs from 'primevue/tabs';
import TabList from 'primevue/tablist';
import Tab from 'primevue/tab';
import Select from 'primevue/select';
import Popover from 'primevue/popover';
import Files from './Files.vue';
import { appearance, customReadable, resetAppearance, terminalColors, terminalDecoration, terminals, workspaces } from './appearance.js';

const props=defineProps({
  controls:{type:Object,required:true},
  terminals:{type:HTMLElement,required:true},
  tabs:{type:HTMLElement,required:true},
  operations:{type:HTMLElement,required:true},
});
const state=shallowRef(null);
const settings=ref(null), splitMenu=ref(null);
const splitLayouts=[
  {value:'auto',label:'自动平铺'},
  {value:'columns',label:'左右分屏'},
  {value:'rows',label:'上下分屏'},
  {value:'single',label:'单窗口'},
];
function pickSplit(value) {
  splitMenu.value.hide();
  props.controls.setSplitLayout(value);
}
const leftView=ref('servers');
const narrowViewport=matchMedia('(max-width:820px)');
const narrow=ref(narrowViewport.matches);
const connectionsOpen=ref(!narrow.value);
const operationsOpen=ref(true);
const operationsVisible=computed(()=>narrow.value?Boolean(state.value?.sidebarOpen):operationsOpen.value);
const terminalDock=ref(null), tabsDock=ref(null), operationsDock=ref(null);
const connections=computed(()=>(state.value?.connections||[]).filter(connection=>connection.kind!=='nested'));
const operationTab={root:{'aria-controls':'sidebar-content'}};
let unsubscribe;

function updateViewport(event) {
  narrow.value=event.matches;
}
async function toggleConnections() {
  connectionsOpen.value=!connectionsOpen.value;
  await nextTick();
  document.getElementById(connectionsOpen.value?'collapse-connections':'expand-connections')?.focus();
}
async function toggleOperations() {
  const open=!operationsVisible.value;
  if(narrow.value)props.controls.setSidebarOpen(open);
  else operationsOpen.value=open;
  await nextTick();
  document.getElementById(open?'collapse-operations':'expand-operations')?.focus();
}

function address(connection){
  if(connection.kind==='local')return '工作台所在主机';
  const host=connection.host.includes(':')?'['+connection.host+']':connection.host;
  return host+':'+connection.port;
}
watchEffect(()=>props.controls.setTerminalAppearance(terminalColors.value,terminalDecoration.value));
onMounted(()=>{
  tabsDock.value.append(props.tabs);
  terminalDock.value.append(props.terminals);
  operationsDock.value.append(props.operations);
  narrowViewport.addEventListener('change',updateViewport);
  unsubscribe=props.controls.subscribe(value=>{state.value=value;});
});
onBeforeUnmount(()=>{
  narrowViewport.removeEventListener('change',updateViewport);
  unsubscribe?.();
});
</script>

<template>
  <Teleport to="#aura-tools">
    <div class="aura-tools">
      <Button label="外观" size="small" severity="secondary" outlined aria-haspopup="dialog" @click="settings.toggle($event)" />
    </div>
  </Teleport>
  <Popover ref="settings" class="aura-appearance" aria-label="外观设置">
    <h2>外观设置</h2>
    <div class="aura-settings-field">
      <label id="workspace-theme-label">工作台主题</label>
      <Select v-model="appearance.workspace" :options="workspaces" optionLabel="label" optionValue="value"
              aria-labelledby="workspace-theme-label" appendTo="self" />
    </div>
    <div class="aura-settings-field">
      <label id="terminal-theme-label">终端配色</label>
      <Select v-model="appearance.terminal" :options="terminals" optionLabel="label" optionValue="value"
              aria-labelledby="terminal-theme-label" appendTo="self" />
    </div>
    <div v-if="appearance.terminal==='custom'" class="aura-custom-colors">
      <label><span>背景色</span><input v-model="appearance.customBackground" type="color"><code>{{ appearance.customBackground }}</code></label>
      <label><span>文字色</span><input v-model="appearance.customForeground" type="color"><code>{{ appearance.customForeground }}</code></label>
      <p v-if="!customReadable" class="error" role="status">文字和背景的对比较低。</p>
    </div>
    <div class="aura-settings-actions">
      <Button label="恢复默认" severity="secondary" size="small" text @click="resetAppearance" />
      <Button label="完成" size="small" @click="settings.hide()" />
    </div>
  </Popover>

  <div class="aura-dock">
    <Button v-show="!connectionsOpen" id="expand-connections" class="aura-edge-toggle aura-edge-left"
            size="small" severity="secondary" outlined aria-label="展开左侧面板" title="展开左侧面板"
            aria-controls="aura-connections" aria-expanded="false" @click="toggleConnections">
      <span aria-hidden="true">›</span>
    </Button>
    <Splitter class="aura-splitter" :class="{'hide-connections':!connectionsOpen,'hide-operations':!operationsVisible}" :gutterSize="7">
      <SplitterPanel id="aura-connections" :size="22" :minSize="17" class="aura-connections">
        <div class="aura-pane-title">
          <Button id="collapse-connections" class="aura-collapse-toggle" size="small" severity="secondary" text
                  aria-label="收起左侧面板" title="收起左侧面板" aria-controls="aura-connections"
                  aria-expanded="true" @click="toggleConnections"><span aria-hidden="true">‹</span></Button>
          <Tabs v-model:value="leftView" class="aura-file-tabs" aria-label="连接区">
            <TabList><Tab value="servers">服务器</Tab><Tab value="files">文件</Tab></TabList>
          </Tabs>
          <Button v-if="leftView==='servers'" label="添加连接" size="small" severity="secondary" outlined @click="controls.addConnection()" />
        </div>
        <div v-if="leftView==='servers'" class="aura-connection-list" aria-label="已保存的服务器连接">
          <div v-for="connection in connections" :key="connection.id" class="aura-connection"
               :class="{selected:state?.connection===connection.id}">
            <div>
              <strong>{{ connection.name }}</strong>
              <p>{{ address(connection) }} · {{ connection.user }}</p>
            </div>
            <Button :label="connection.kind==='local'?'打开':'连接'" size="small"
                    :severity="state?.connection===connection.id?'primary':'secondary'"
                    :aria-label="connection.kind==='local'?'本机终端':'连接 '+connection.name"
                    @click="controls.connect(connection.id)" />
          </div>
          <p v-if="!connections.length" class="aura-empty">添加 SSH 连接后，可以在这里打开终端。</p>
        </div>
        <Files :state="state" :controls="controls" :visible="leftView==='files'&&connectionsOpen" />
      </SplitterPanel>
      <SplitterPanel :size="55" :minSize="32" class="aura-terminals">
        <div class="aura-terminal-toolbar">
          <div ref="tabsDock" class="aura-terminal-tabs"></div>
          <Button label="分屏" size="small" severity="secondary" text aria-haspopup="dialog"
                  :disabled="!state||state.terminalCount<2" @click="splitMenu.toggle($event)">
            <span class="split-control-icon" aria-hidden="true"></span><span>分屏</span>
          </Button>
          <Popover ref="splitMenu" class="split-layout-menu" aria-label="终端分屏布局">
            <Button v-for="layout in splitLayouts" :key="layout.value" :label="layout.label" size="small"
                    :severity="state?.splitLayout===layout.value?'primary':'secondary'" text
                    :aria-pressed="state?.splitLayout===layout.value" @click="pickSplit(layout.value)" />
            <small>拖动终端标签到另一窗格的边缘。</small>
          </Popover>
        </div>
        <div class="aura-terminal-body" ref="terminalDock"></div>
      </SplitterPanel>
      <SplitterPanel :size="23" :minSize="17" class="aura-operations">
        <div ref="operationsDock" class="aura-operation-body"></div>
      </SplitterPanel>
    </Splitter>
    <Button v-show="!operationsVisible" id="expand-operations" class="aura-edge-toggle aura-edge-right"
            size="small" severity="secondary" outlined aria-label="展开快捷操作" title="展开快捷操作"
            aria-controls="terminal-sidebar" aria-expanded="false" @click="toggleOperations">
      <span aria-hidden="true">‹</span><span v-if="narrow">快捷操作</span>
    </Button>
  </div>

  <Teleport to="#aura-operations-toggle">
    <Button id="collapse-operations" class="aura-collapse-toggle" size="small" severity="secondary" text
            aria-label="收起快捷操作" title="收起快捷操作" aria-controls="terminal-sidebar"
            aria-expanded="true" @click="toggleOperations"><span aria-hidden="true">›</span></Button>
  </Teleport>
  <Teleport to="#aura-operation-tabs">
    <Tabs :value="state?.side||'flows'" class="aura-operation-tabs" @update:value="controls.showSide($event)">
      <TabList>
        <Tab value="commands" :pt="operationTab">快捷指令</Tab>
        <Tab value="flows" :pt="operationTab">自动化流程</Tab>
        <Tab value="history" :pt="operationTab">执行记录</Tab>
      </TabList>
    </Tabs>
  </Teleport>
  <div id="aura-transfers"></div>
  <footer class="aura-status" aria-label="当前终端状态">
    <Tag severity="secondary" :value="(state?.openCount||0)+' 个终端'" />
    <span v-if="state?.terminalName">{{ state.terminalName }}</span>
    <span v-if="state?.user">{{ state.user }}</span>
    <code v-if="state?.directory" :title="state.directory">{{ state.directory }}</code>
    <span v-if="state?.terminalStatus" class="aura-terminal-status">{{ state.terminalStatus }}</span>
  </footer>
</template>
