<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import Button from 'primevue/button';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import ProgressBar from 'primevue/progressbar';

const props=defineProps({ state:Object, visible:Boolean, controls:{type:Object,required:true} });
const folders=reactive(new Map());
const jobs=reactive(new Map());
const request=(path,data)=>props.controls.request(path,data);
const scope=computed(()=>{
  const { terminalId, connection, user }=props.state||{};
  return terminalId&&connection&&user?JSON.stringify([terminalId,connection,user]):null;
});
const folder=computed(()=>folders.get(scope.value));
const pathInput=ref('');
const breadcrumbNav=ref(null);
const fileInput=ref(null);
const transferPanel=ref(false);
const apiError=ref('');
const dialog=ref(null);
const field=ref('');
const dialogError=ref('');
const submitting=ref(false);
let resolveClose;
let uploadDestination;
let updating=false;
let timer;
const statusText={running:'传输中',succeeded:'完成',cancelled:'已取消',failed:'失败',conflict:'文件冲突'};
const activeCount=computed(()=>[...jobs.values()].filter(job=>!job.released||job.status==='running').length);
const canBrowse=computed(()=>props.state?.filesReady&&!folder.value?.loading);
const canTransfer=computed(()=>canBrowse.value&&Boolean(folder.value?.directory));
const breadcrumbs=computed(()=>{
  const parts=(folder.value?.directory||'').split('/').filter(Boolean);
  return [{name:'/',path:'/'},...parts.map((name,i)=>({name,path:'/'+parts.slice(0,i+1).join('/')}))];
});

function target(directory) {
  return {terminal:props.state.terminalId,connection:props.state.connection,user:props.state.user,
    directory,scope:scope.value,server:props.state.terminalName};
}

function sameTarget(destination) {
  return destination.scope===scope.value;
}

function bytes(value) {
  const units=['B','KB','MB','GB','TB','PB'];
  let i=0;
  while(value>=1024&&i<units.length-1) { value/=1024; i++; }
  return `${i?value.toFixed(1):value} ${units[i]}`;
}

function pathFor(directory,name) {
  return directory.replace(/\/$/,'')+'/'+name;
}

async function browse(directory) {
  if(!directory||!canBrowse.value||!scope.value)return;
  const destination=target(directory);
  if(!folder.value)folders.set(destination.scope,{directory:'',home:'',entries:[],loading:false,error:''});
  const view=folders.get(destination.scope);
  view.loading=true;
  view.error='';
  apiError.value='';
  try{
    const result=await request('files',{mode:'list',...destination});
    Object.assign(view,result);
    if(sameTarget(destination))pathInput.value=result.directory;
  }catch(error){
    view.error=error.message;
  }finally{
    view.loading=false;
  }
}

watch(scope,()=>{pathInput.value=folder.value?.directory||''; apiError.value='';});
watch([()=>folder.value?.directory,()=>props.visible],()=>{
  const nav=breadcrumbNav.value;
  if(nav)nav.scrollLeft=nav.scrollWidth;
},{flush:'post'});
watch([scope,()=>props.visible,()=>props.state?.filesReady,()=>props.state?.workspace],()=>{
  if(props.visible&&props.state?.workspace&&!folder.value){
    void browse(props.state.directory);
  }else if(props.visible&&folder.value?.refresh&&canBrowse.value){
    folder.value.refresh=false;
    void browse(folder.value.directory);
  }
},{flush:'post'});

async function updateJobs() {
  if(updating)return;
  const pending=[...jobs.values()].filter(job=>!job.released||job.status==='running');
  if(!pending.length)return;
  updating=true;
  try {
    await Promise.all(pending.map(async job=>{
      try{
        const status=await request(`files/${job.id}/status`);
        jobs.set(job.id,{...job,...status});
        if(status.released&&status.direction==='upload'&&status.status==='succeeded'){
          const view=folders.get(job.scope);
          if(view?.directory===job.directory){
            if(sameTarget(job)&&props.visible&&canBrowse.value)void browse(job.directory);
            else view.refresh=true;
          }
        }
      }catch(error){
        if([401,404].includes(error.status))jobs.set(job.id,{...job,released:true,status:'failed',error:error.message});
      }
    }));
  } finally {updating=false;}
}

function track(job,destination) {
  jobs.set(job.id,{...job,scope:destination.scope});
  const completed=[...jobs.values()].filter(entry=>entry.released&&entry.status!=='running');
  for(const entry of completed.slice(0,-8))jobs.delete(entry.id);
  transferPanel.value=true;
}

async function cancel(job) {
  try {
    await request(`files/${job.id}/cancel`,{});
    await updateJobs();
  }catch(error){apiError.value=error.message;}
}

async function prepareUpload(file,destination,name=file.name,version=null) {
  submitting.value=true;
  dialogError.value='';
  apiError.value='';
  try {
    const job=await request('files',{mode:'upload',...destination,name,size:file.size,overwrite:Boolean(version),version});
    dialog.value=null;
    track(job,destination);
    void sendFile(job,file);
  }catch(error){
    if(error.status===409){
      try {
        const result=await request('files',{mode:'inspect',...destination,name});
        field.value=name;
        dialog.value={type:'conflict',file,destination,name,version:result.version};
      }catch(inspectError){apiError.value=inspectError.message;}
    }else if(dialog.value){dialogError.value=error.message;}
    else {apiError.value=error.message;}
  }finally{submitting.value=false;}
}

async function sendFile(job,file) {
  try{
    const response=await fetch(`/api/files/${job.id}/upload`,{method:'POST',body:file});
    if(!response.ok)throw new Error((await response.json()).error);
  }catch(error){
    const current=jobs.get(job.id);
    if(current?.status==='running')jobs.set(job.id,{...current,error:error.message});
  }finally{await updateJobs();}
}

function openPicker() {
  uploadDestination=target(folder.value.directory);
  fileInput.value.click();
}

function chooseFile(event) {
  const file=event.target.files[0];
  event.target.value='';
  if(file&&uploadDestination)void prepareUpload(file,uploadDestination);
  uploadDestination=null;
}

async function download(entry) {
  const destination=target(folder.value.directory);
  apiError.value='';
  try{
    const job=await request('files',{mode:'download',...destination,name:entry.name});
    track(job,destination);
    const link=document.createElement('a');
    link.href=`/api/files/${job.id}/download`;
    link.download=entry.name;
    document.body.append(link);
    link.click();
    link.remove();
  }catch(error){apiError.value=error.message;}
}

function createFolder() {
  field.value='';
  dialogError.value='';
  dialog.value={type:'mkdir',destination:target(folder.value.directory)};
}

async function saveFolder() {
  const {destination}=dialog.value;
  submitting.value=true;
  dialogError.value='';
  try {
    await request('files',{mode:'mkdir',...destination,name:field.value});
    dialog.value=null;
    if(sameTarget(destination)){
      await nextTick();
      if(canBrowse.value)await browse(destination.directory);
      else if(folder.value)folder.value.refresh=true;
    }
  }catch(error){dialogError.value=error.message;}
  finally{submitting.value=false;}
}

function closeDialog(confirmed=false) {
  const resolver=resolveClose;
  resolveClose=undefined;
  dialog.value=null;
  resolver?.(confirmed);
}

onMounted(()=>{
  timer=setInterval(updateJobs,450);
  props.controls.onTransferClose(server=>new Promise(resolve=>{
    if(dialog.value){resolve(false);return;}
    dialogError.value='';
    resolveClose=resolve;
    dialog.value={type:'close',server};
  }));
});
onBeforeUnmount(()=>{
  clearInterval(timer);
  resolveClose?.(false);
  props.controls.onTransferClose(null);
});
</script>

<template>
  <section v-if="visible" class="aura-files" aria-label="远程文件">
    <p v-if="!scope" class="aura-empty">打开终端后，即可查看该服务器的文件。</p>
    <template v-else>
      <div class="file-target">
        <strong>{{ state.terminalName }} <span>· {{ state.user }}</span></strong>
        <button class="terminal-directory" :disabled="!canBrowse" :title="state.directory"
                @click="browse(state.directory)">
          <span>终端目录</span>
          <code>{{ state.directory }}</code>
        </button>
      </div>
      <form class="file-path" @submit.prevent="browse(pathInput)">
        <InputText v-model="pathInput" size="small" aria-label="服务器目录路径" placeholder="输入目录路径" spellcheck="false"/>
        <Button type="submit" label="前往" size="small" severity="secondary" outlined :disabled="!canBrowse||!pathInput"/>
      </form>
      <nav v-if="folder?.directory" ref="breadcrumbNav" class="file-breadcrumbs" aria-label="当前目录">
        <template v-for="(part,index) in breadcrumbs" :key="part.path">
          <span v-if="index>1" aria-hidden="true">/</span>
          <button :disabled="!canBrowse" :title="part.path" :aria-current="index===breadcrumbs.length-1?'location':undefined"
                  @click="browse(part.path)">{{ part.name }}</button>
        </template>
      </nav>
      <div class="file-navigation">
        <Button label="上一级" size="small" severity="secondary" text :disabled="!canBrowse||breadcrumbs.length<2"
                @click="browse(breadcrumbs.at(-2).path)"/>
        <Button label="家目录" size="small" severity="secondary" text :disabled="!canBrowse||!folder?.home"
                @click="browse(folder.home)"/>
        <Button label="刷新" size="small" severity="secondary" text :disabled="!canBrowse"
                @click="browse(folder?.directory||state.directory)"/>
      </div>
      <div class="file-actions">
        <Button label="上传文件" size="small" :disabled="!canTransfer" @click="openPicker"/>
        <input ref="fileInput" type="file" hidden @change="chooseFile">
        <Button label="新建文件夹" size="small" severity="secondary" outlined :disabled="!canTransfer" @click="createFolder"/>
      </div>
      <p v-if="apiError||folder?.error" class="file-error" role="alert">{{ apiError||folder.error }}</p>
      <p v-else-if="folder?.loading" class="file-notice" role="status">正在读取目录…</p>
      <p v-else-if="!state.filesReady" class="file-notice" role="status">终端空闲并返回命令提示符后，可以操作文件。</p>
      <ul v-if="folder?.entries.length" class="file-list" aria-label="目录内容">
        <li v-for="entry in folder.entries" :key="entry.name">
          <button v-if="entry.kind==='directory'" class="file-item folder-item" :disabled="!canBrowse" :title="entry.name"
                  @click="browse(pathFor(folder.directory,entry.name))">
            <span class="folder-mark" aria-hidden="true"></span><span class="file-name">{{ entry.name }}</span><span aria-hidden="true">›</span>
          </button>
          <div v-else class="file-item" :title="entry.name">
            <span class="file-mark" aria-hidden="true"></span>
            <span class="file-name">{{ entry.name }}</span>
            <span v-if="entry.kind==='file'" class="file-size">{{ bytes(entry.size) }}</span>
            <Button v-if="entry.kind==='file'" label="下载" size="small" severity="secondary" text
                    :aria-label="'下载 '+entry.name" :disabled="!canTransfer" @click="download(entry)"/>
          </div>
        </li>
      </ul>
      <p v-else-if="folder?.directory&&!folder.loading" class="aura-empty">这个目录是空的。</p>
    </template>
  </section>

  <Teleport defer to="#aura-transfers">
    <section class="file-dock" aria-label="文件传输">
      <Button class="file-dock-toggle" label="传输" size="small" severity="secondary" text
              :aria-expanded="transferPanel" aria-controls="file-jobs"
              @click="transferPanel=!transferPanel">
        <span aria-hidden="true">{{ transferPanel?'⌄':'›' }}</span>
        <span>传输</span><span v-if="activeCount" class="transfer-count">{{ activeCount }}</span>
      </Button>
      <div v-show="transferPanel" id="file-jobs" class="file-jobs">
        <p v-if="!jobs.size" class="aura-empty">上传或下载时，可以在这里查看进度。</p>
        <article v-for="job in [...jobs.values()].reverse()" :key="job.id" class="file-job">
          <div class="file-job-details">
            <strong :title="job.name">{{ job.direction==='upload'?'上传':'下载' }} · {{ job.name }}</strong>
            <code :title="pathFor(job.directory,job.name)">{{ job.server }} · {{ job.user }} · {{ pathFor(job.directory,job.name) }}</code>
          </div>
          <div class="file-job-progress">
            <ProgressBar v-if="job.status==='running'&&job.size" :value="Math.floor(job.transferred/job.size*100)"
                         :showValue="false" :aria-label="job.name+' 传输进度'"/>
            <small>{{ statusText[job.status] }}<template v-if="job.status==='running'&&job.size">
              · {{ bytes(job.transferred) }} / {{ bytes(job.size) }}</template></small>
            <small v-if="job.error" class="file-error">{{ job.error }}</small>
          </div>
          <Button v-if="job.status==='running'&&!job.released" label="取消传输" size="small" severity="secondary" text @click="cancel(job)"/>
        </article>
      </div>
    </section>
  </Teleport>

  <Dialog :visible="Boolean(dialog)" modal :closable="!submitting" :dismissableMask="false"
          :header="dialog?.type==='close'?'关闭终端':dialog?.type==='conflict'?(dialog.version?'目标目录已有同名文件':'原文件已移除'):'新建文件夹'"
          :style="{width:'min(30rem, 93vw)'}" @update:visible="visible=>{if(!visible)closeDialog();}">
    <template v-if="dialog?.type==='close'">
      <p>关闭 {{ dialog.server }} 会中断正在进行的文件传输。</p>
    </template>
    <template v-else-if="dialog">
      <p class="file-dialog-path">
        {{ dialog.destination.server }} · {{ dialog.destination.user }}<br>
        <code>{{ dialog.type==='conflict'?pathFor(dialog.destination.directory,dialog.name):dialog.destination.directory }}</code>
      </p>
      <form id="file-name-form" class="file-dialog-field" @submit.prevent="dialog.type==='mkdir'?saveFolder():prepareUpload(dialog.file,dialog.destination,field)">
        <label for="file-new-name">{{ dialog.type==='mkdir'?'文件夹名称':'另存的文件名' }}</label>
        <InputText v-model="field" id="file-new-name" :disabled="submitting" autocomplete="off"/>
      </form>
    </template>
    <p v-if="dialogError" class="file-error" role="alert">{{ dialogError }}</p>
    <template #footer>
      <template v-if="dialog?.type==='close'">
        <Button label="保留终端" severity="secondary" text @click="closeDialog()"/>
        <Button label="中断传输并关闭" severity="danger" @click="closeDialog(true)"/>
      </template>
      <template v-else-if="dialog">
        <Button label="取消" severity="secondary" text :disabled="submitting" @click="closeDialog()"/>
        <Button v-if="dialog.type==='conflict'" label="覆盖原文件" severity="danger" outlined :disabled="submitting||!dialog.version"
                @click="prepareUpload(dialog.file,dialog.destination,dialog.name,dialog.version)"/>
        <Button type="submit" form="file-name-form" :label="dialog.type==='mkdir'?'创建':dialog.version?'改名上传':'上传'" :loading="submitting"
                :disabled="!field||dialog.type==='conflict'&&dialog.version&&field===dialog.name"/>
      </template>
    </template>
  </Dialog>
</template>
