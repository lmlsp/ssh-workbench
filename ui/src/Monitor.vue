<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import Button from 'primevue/button';
import Tag from 'primevue/tag';

const props=defineProps({
  state:{type:Object,default:null},
  controls:{type:Object,required:true},
  visible:{type:Boolean,default:false},
});
const servers=ref([]), loading=ref(false), error=ref(''), generatedAt=ref('');
const refreshAfter=ref(12);
const onlineCount=computed(()=>servers.value.filter(server=>server.status==='online').length);
const offlineCount=computed(()=>servers.value.filter(server=>server.status==='offline').length);
let timer=null, inFlight=false;

async function refresh() {
  if(!props.visible||inFlight)return;
  inFlight=true;
  if(!servers.value.length)loading.value=true;
  error.value='';
  try {
    const result=await props.controls.request('monitor');
    servers.value=result.servers||[];
    generatedAt.value=result.generatedAt||'';
    refreshAfter.value=result.refreshAfter||12;
  } catch (cause) {
    error.value=cause.message||'读取服务器监控失败';
  } finally {
    loading.value=false;
    inFlight=false;
  }
}

function bytes(value) {
  if(!Number.isFinite(value)||value<=0)return '0 B';
  const units=['B','KB','MB','GB','TB'];
  const index=Math.min(Math.floor(Math.log(value)/Math.log(1024)),units.length-1);
  return `${(value/1024**index).toFixed(index?1:0)} ${units[index]}`;
}
function rate(value) { return `${bytes(value)}/s`; }
function duration(value) {
  const seconds=Math.max(0,Number(value)||0), days=Math.floor(seconds/86400), hours=Math.floor(seconds%86400/3600), minutes=Math.floor(seconds%3600/60);
  return days?`${days} 天 ${hours} 小时`:hours?`${hours} 小时 ${minutes} 分钟`:`${minutes} 分钟`;
}
function percent(value) { return `${Math.max(0,Math.min(100,Number(value)||0)).toFixed(0)}%`; }
function statusText(status) { return status==='online'?'在线':status==='offline'?'离线':'暂不支持'; }
function severity(status) { return status==='online'?'success':status==='offline'?'danger':'secondary'; }
function timeText(value) {
  if(!value)return '—';
  const date=new Date(value);
  return Number.isNaN(date.getTime())?value:date.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
}
async function openTerminal(server) {
  if(!server.connection)return;
  if(props.controls.focusConnection?.(server.connection))return;
  await props.controls.connect(server.connection);
}
watch(()=>props.visible,visible=>{
  if(timer){clearInterval(timer);timer=null;}
  if(visible){void refresh();timer=setInterval(()=>void refresh(),15000);}
},{immediate:true});
onBeforeUnmount(()=>{if(timer)clearInterval(timer);});
</script>

<template>
  <section class="aura-monitor" aria-label="服务器监控">
    <header class="aura-monitor-heading">
      <div><strong>服务器监控</strong><small>{{ servers.length }} 台服务器 · 自动刷新</small></div>
      <Button label="刷新" size="small" severity="secondary" text :loading="loading" @click="refresh" />
    </header>
    <div class="aura-monitor-summary" aria-live="polite">
      <span><i class="monitor-dot online"></i>在线 {{ onlineCount }}</span>
      <span><i class="monitor-dot offline"></i>离线 {{ offlineCount }}</span>
      <span class="monitor-updated">更新 {{ timeText(generatedAt) }}</span>
    </div>
    <p v-if="error" class="aura-monitor-message error" role="alert">{{ error }}</p>
    <p v-else-if="loading&&!servers.length" class="aura-monitor-message" role="status">正在采集服务器指标…</p>
    <p v-else-if="!servers.length" class="aura-monitor-message">暂无已保存的服务器连接。</p>

    <article v-for="server in servers" :key="server.id" class="aura-monitor-card">
      <header class="aura-monitor-card-head">
        <div class="aura-monitor-server-title">
          <strong>{{ server.name }}</strong>
          <Tag :value="statusText(server.status)" :severity="severity(server.status)" />
        </div>
        <Button v-if="server.connection" label="终端" size="small" severity="secondary" text
                :aria-label="'打开 '+server.name+' 终端'" @click="openTerminal(server)" />
      </header>
      <p class="aura-monitor-endpoint">{{ server.host }}<template v-if="server.port">:{{ server.port }}</template>
        <template v-if="server.user"> · {{ server.user }}</template></p>
      <p v-if="server.error" class="aura-monitor-message">{{ server.error }}</p>
      <template v-else-if="server.status==='online'">
        <div class="aura-monitor-metrics">
          <div class="aura-monitor-metric">
            <div><span>CPU</span><b>{{ percent(server.cpuPercent) }}</b></div>
            <div class="monitor-track"><i :style="{width:percent(server.cpuPercent)}"></i></div>
            <small>{{ server.cpuCores }} vCPU</small>
          </div>
          <div class="aura-monitor-metric">
            <div><span>内存</span><b>{{ percent(server.memoryPercent) }}</b></div>
            <div class="monitor-track"><i class="memory" :style="{width:percent(server.memoryPercent)}"></i></div>
            <small>{{ bytes(server.memoryUsed) }} / {{ bytes(server.memoryTotal) }}</small>
          </div>
          <div class="aura-monitor-metric">
            <div><span>Swap</span><b>{{ percent(server.swapPercent) }}</b></div>
            <div class="monitor-track"><i class="swap" :style="{width:percent(server.swapPercent)}"></i></div>
            <small>{{ bytes(server.swapUsed) }} / {{ bytes(server.swapTotal) }}</small>
          </div>
          <div class="aura-monitor-metric monitor-load">
            <div><span>系统负载</span><b>{{ server.load?.[0]??'—' }}</b></div>
            <small>1 / 5 / 15 分钟</small>
            <strong>{{ server.load?.join(' / ')||'—' }}</strong>
          </div>
        </div>

        <section v-if="server.disks?.length" class="aura-monitor-section">
          <header><strong>磁盘分区</strong><small>{{ server.disks.length }} 个挂载点</small></header>
          <div v-for="disk in server.disks" :key="disk.mount" class="aura-monitor-disk">
            <div><b>{{ disk.mount }}</b><span>{{ bytes(disk.used) }} / {{ bytes(disk.total) }}</span><strong>{{ disk.percent }}%</strong></div>
            <div class="monitor-track"><i :class="{warning:disk.percent>=80}" :style="{width:percent(disk.percent)}"></i></div>
          </div>
        </section>

        <div class="aura-monitor-io">
          <div><span>↓ 网络接收</span><b>{{ rate(server.networkRxRate) }}</b><small>累计 {{ bytes(server.networkRxBytes) }}</small></div>
          <div><span>↑ 网络发送</span><b>{{ rate(server.networkTxRate) }}</b><small>累计 {{ bytes(server.networkTxBytes) }}</small></div>
          <div><span>磁盘读取</span><b>{{ rate(server.diskReadRate) }}</b></div>
          <div><span>磁盘写入</span><b>{{ rate(server.diskWriteRate) }}</b></div>
        </div>
        <div class="aura-monitor-system">
          <span>运行 {{ duration(server.uptimeSeconds) }}</span><span>{{ server.os }}</span>
          <span>进程 {{ server.processes }}</span><span>TCP {{ server.tcpConnections }}</span>
        </div>
        <footer class="aura-monitor-card-foot">内核 {{ server.kernel }} · 采集 {{ timeText(server.sampledAt) }}</footer>
      </template>
    </article>
  </section>
</template>
