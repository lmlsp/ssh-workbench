import { computed, reactive, watch } from 'vue';
import Aura from '@primeuix/themes/aura';
import { definePreset, usePreset } from '@primeuix/themes';

export const workspaces = [
  {label:'柔和浅蓝', value:'blue'},
  {label:'中性浅色', value:'neutral'},
  {label:'深色石墨', value:'graphite'},
];
export const terminals = [
  {label:'柔和灰蓝', value:'slate'},
  {label:'Ubuntu 茄紫', value:'ubuntu'},
  {label:'石墨深色', value:'graphite'},
  {label:'低对比浅色', value:'light'},
  {label:'自定义', value:'custom'},
];
const storageKey='ssh-workbench.appearance.v1';
const hex=/^#[\da-f]{6}$/i;
const baseDark={green:'#89c8ae',brightGreen:'#93cfb8',blue:'#96b9df',brightBlue:'#b1cfee'};
const baseLight={green:'#37624f',brightGreen:'#285f47',blue:'#2f5183',brightBlue:'#315b8e'};
const terminalPresets={
  slate:{...baseDark,background:'#28384a',foreground:'#e5edf6'},
  ubuntu:{background:'#300a24',foreground:'#eeeeec',green:'#8ae234',brightGreen:'#a7e45f',blue:'#729fcf',brightBlue:'#a4c6ef',
    red:'#cc0000',brightRed:'#ef2929',yellow:'#c4a000',brightYellow:'#fce94f',magenta:'#75507b',brightMagenta:'#ad7fa8'},
  graphite:{...baseDark,background:'#282e34',foreground:'#e4eaf0'},
  light:{...baseLight,background:'#eef3f7',foreground:'#344254'},
};
const defaults={workspace:'blue',terminal:'slate',customBackground:terminalPresets.slate.background,customForeground:terminalPresets.slate.foreground};

function readSettings(){
  try{
    const saved=JSON.parse(localStorage.getItem(storageKey))||{};
    return {
      workspace:workspaces.some(item=>item.value===saved.workspace)?saved.workspace:defaults.workspace,
      terminal:terminals.some(item=>item.value===saved.terminal)?saved.terminal:defaults.terminal,
      customBackground:hex.test(saved.customBackground)?saved.customBackground:defaults.customBackground,
      customForeground:hex.test(saved.customForeground)?saved.customForeground:defaults.customForeground,
    };
  }catch{ return {...defaults}; }
}
export const appearance=reactive(readSettings());

const blue={
  50:'#edf6ff',100:'#deedfc',200:'#c4def8',300:'#a2c8ef',400:'#77a9de',500:'#508ac6',
  600:'#3a74b3',700:'#295f9b',800:'#234f81',900:'#1c416c',950:'#172f4d',
};
const neutral={
  50:'#f0f3f5',100:'#e4e9ed',200:'#ccd6df',300:'#aebdcb',400:'#889aa9',500:'#6a7f92',
  600:'#53697d',700:'#40576b',800:'#324458',900:'#26354a',950:'#182534',
};
function preset(primary){
  return definePreset(Aura,{
    primitive:{borderRadius:{none:'0',xs:'4px',sm:'6px',md:'9px',lg:'12px',xl:'16px'}},
    semantic:{primary,colorScheme:{
      light:{
        primary:{color:'{primary.700}',hoverColor:'{primary.800}',activeColor:'{primary.900}'},
        highlight:{background:'{primary.50}',focusBackground:'{primary.100}',color:'{primary.900}',focusColor:'{primary.950}'}
      },
      dark:{
        primary:{color:'{primary.300}',hoverColor:'{primary.200}',activeColor:'{primary.100}',contrastColor:'{primary.950}'},
        highlight:{background:'{primary.800}',focusBackground:'{primary.700}',color:'{primary.100}',focusColor:'{primary.50}'}
      },
    }}
  });
}
const bluePreset=preset(blue), neutralPreset=preset(neutral);
export function workspacePreset(){ return appearance.workspace==='neutral'?neutralPreset:bluePreset; }
export function applyWorkspace(){
  document.documentElement.dataset.workspaceTheme=appearance.workspace;
  document.documentElement.classList.toggle('theme-dark',appearance.workspace==='graphite');
}
watch(()=>appearance.workspace,()=>{
  applyWorkspace();
  usePreset(workspacePreset());
});
watch(appearance,value=>{
  try{localStorage.setItem(storageKey,JSON.stringify(value));}catch{}
});
export function resetAppearance(){Object.assign(appearance,defaults);}

function rgb(color){
  return color.slice(1).match(/../g).map(channel=>parseInt(channel,16));
}
function blend(a,b,weight){
  const next=rgb(b);
  return '#'+rgb(a).map((channel,index)=>Math.round(channel+(next[index]-channel)*weight).toString(16).padStart(2,'0')).join('');
}
function luminance(color){
  const channels=rgb(color).map(channel=>channel/255).map(value=>value<=.04045?value/12.92:((value+.055)/1.055)**2.4);
  return channels[0]*.2126+channels[1]*.7152+channels[2]*.0722;
}
export const terminalColors=computed(()=>{
  let colors=terminalPresets[appearance.terminal];
  if(appearance.terminal==='custom'){
    const background=appearance.customBackground,foreground=appearance.customForeground;
    colors={...(luminance(background)>.42?baseLight:baseDark),background,foreground};
  }
  return {...colors,cursor:colors.foreground,cursorAccent:colors.background,selectionBackground:blend(colors.background,colors.foreground,.24)};
});
export const terminalDecoration=computed(()=>{
  const {background,foreground}=terminalColors.value;
  return {
    '--terminal':background,'--terminal-chrome':blend(background,foreground,.105),'--terminal-ink':foreground,
    '--terminal-muted':blend(foreground,background,.19),'--terminal-line':blend(background,foreground,.30),
    '--terminal-scrollbar':blend(background,foreground,.48),
  };
});
export const customReadable=computed(()=>{
  const levels=[luminance(appearance.customBackground),luminance(appearance.customForeground)].sort((a,b)=>b-a);
  return (levels[0]+.05)/(levels[1]+.05)>=4.5;
});
