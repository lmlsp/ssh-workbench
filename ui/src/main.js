import { createApp } from 'vue';
import PrimeVue from 'primevue/config';
import Workspace from './Workspace.vue';
import { applyWorkspace, workspacePreset } from './appearance.js';
import './workspace.css';

function mountWorkspace() {
  const controls = window.workbenchControls;
  if (!controls) return;
  const terminals = document.querySelector('.terminal-stack');
  const tabs = document.querySelector('#terminal-tabs');
  const operations = document.querySelector('#terminal-sidebar');
  const layout = document.querySelector('.terminal-desk');
  const toolbar = document.querySelector('.terminal-toolbar');
  const title = document.querySelector('#view-terminal .page-heading');
  const empty = document.querySelector('#terminal-empty');

  applyWorkspace();
  createApp(Workspace, { controls, terminals, tabs, operations }).use(PrimeVue, {
    ripple: false,
    theme: { preset: workspacePreset(), options: { darkModeSelector: '.theme-dark' } },
  }).mount('#aura-workspace');

  // Keep the established controllers' DOM anchors, even when the new controls take their place.
  for (const element of [layout, toolbar, title, empty]) element.classList.add('aura-legacy');
  document.documentElement.classList.add('has-aura');
}
if (window.workbenchControls) mountWorkspace();
else window.addEventListener('workbench:controls-ready', mountWorkspace, { once: true });
