<script setup>
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watchEffect } from 'vue';
import Button from 'primevue/button';
import Tag from 'primevue/tag';
import Splitter from 'primevue/splitter';
import SplitterPanel from 'primevue/splitterpanel';
import Tabs from 'primevue/tabs';
import TabList from 'primevue/tablist';
import Tab from 'primevue/tab';
import Select from 'primevue/select';
import Popover from 'primevue/popover';
import { appearance, customReadable, resetAppearance, terminalColors, terminalDecoration, terminals, workspaces } from './appearance.js';

const props=defineProps({
  controls:{type:Object,required:true},
  terminals:{type:HTMLElement,required:true},
  tabs:{type:HTMLElement,required:true},
  operations:{type:HTMLElement,required:true},
});
const state=shallowRef(null);
const settings=ref(null);
const connectionsOpen=ref(!matchMedia('(max-width:820px)').matches);
const operationsOpen=ref(true);
const terminalDock=ref(null), tabsDock=ref(null), operationsDock=ref(null);
const connections=computed(()=>(state.value?.connections||[]).filter(connection=>connection.kind!=='nested'));
const operationTab={root:{'aria-controls':'sidebar-content'}};
let unsubscribe;

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
  unsubscribe=props.controls.subscribe(value=>{state.value=value;});
});
onBeforeUnmount(()=>unsubscribe?.());
</script>

<template>
  <Teleport to="#aura-tools">
    <div class="aura-tools">
      <Button v-if="state?.workspace" label="连接区" size="small" severity="secondary"
              :outlined="!connectionsOpen" aria-controls="aura-connections" :aria-pressed="connectionsOpen"
              @click="connectionsOpen=!connectionsOpen" />
      <Button v-if="state?.workspace" class="aura-desktop-actions" label="快捷操作" size="small" severity="secondary"
              :outlined="!operationsOpen" aria-controls="terminal-sidebar" :aria-pressed="operationsOpen"
              @click="operationsOpen=!operationsOpen" />
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

  <Splitter class="aura-splitter" :class="{'hide-connections':!connectionsOpen,'hide-operations':!operationsOpen}" :gutterSize="7">
    <SplitterPanel :size="22" :minSize="17" class="aura-connections">
      <div id="aura-connections" class="aura-pane-title">
        <h2>服务器</h2>
        <Button label="添加连接" size="small" severity="secondary" outlined @click="controls.addConnection()" />
      </div>
      <div class="aura-connection-list" aria-label="已保存的服务器连接">
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
    </SplitterPanel>
    <SplitterPanel :size="55" :minSize="32" class="aura-terminals">
      <div class="aura-terminal-toolbar">
        <div ref="tabsDock" class="aura-terminal-tabs"></div>
        <Button :label="state?.tiled?'单窗口':'平铺窗口'" size="small" severity="secondary" text
                :disabled="!state||state.terminalCount<2" :aria-pressed="Boolean(state?.tiled)" @click="controls.toggleTiles()" />
      </div>
      <div class="aura-terminal-body" ref="terminalDock"></div>
    </SplitterPanel>
    <SplitterPanel :size="23" :minSize="17" class="aura-operations">
      <div ref="operationsDock" class="aura-operation-body"></div>
    </SplitterPanel>
  </Splitter>

  <Teleport to="#aura-operation-tabs">
    <Tabs :value="state?.side||'flows'" class="aura-operation-tabs" @update:value="controls.showSide($event)">
      <TabList>
        <Tab value="commands" :pt="operationTab">快捷指令</Tab>
        <Tab value="flows" :pt="operationTab">自动化流程</Tab>
        <Tab value="history" :pt="operationTab">执行记录</Tab>
      </TabList>
    </Tabs>
  </Teleport>
  <footer class="aura-status" aria-label="当前终端状态">
    <Tag severity="secondary" :value="(state?.openCount||0)+' 个终端'" />
    <span v-if="state?.terminalName">{{ state.terminalName }}</span>
    <span v-if="state?.user">{{ state.user }}</span>
    <code v-if="state?.directory" :title="state.directory">{{ state.directory }}</code>
    <span v-if="state?.terminalStatus" class="aura-terminal-status">{{ state.terminalStatus }}</span>
  </footer>
</template>
