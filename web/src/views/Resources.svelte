<script>
  import { fade, scale } from 'svelte/transition';
  import { flip } from 'svelte/animate';
  import { API } from '../api.js';
  import Icon from '../icons/Icon.svelte';

  export let resourceFilter = 'all';
  export let onBack;

  let resources = [];
  let loadingResources = false;
  let statusMsg = '';

  async function loadResources() {
    loadingResources = true;
    statusMsg = '';
    const all = await API.fetchAllInstances();
    resources = resourceFilter === 'running'
      ? all.filter(r => r.state === 'running')
      : all;
    statusMsg = resources.length
      ? `Found ${resources.length} resource(s)`
      : 'No resources found';
    loadingResources = false;
  }

  async function startOne(r) {
    const prevState = r.state;
    r.state = 'starting';
    resources = resources;
    statusMsg = `Starting ${r.name}...`;
    const ok = await API.startInstance(r);
    if (!ok) {
      r.state = prevState;
      resources = resources;
      statusMsg = `Failed to start ${r.name}`;
      return;
    }
    setTimeout(loadResources, 3000);
  }

  async function stopOne(r) {
    const prevState = r.state;
    r.state = 'stopping';
    resources = resources;
    statusMsg = `Stopping ${r.name}...`;
    const ok = await API.stopInstance(r);
    if (!ok) {
      r.state = prevState;
      resources = resources;
      statusMsg = `Failed to stop ${r.name}`;
      return;
    }
    setTimeout(loadResources, 3000);
  }

  async function startAll() {
    if (!resources.some(r => r.state === 'stopped')) {
      statusMsg = 'No stopped resources';
      return;
    }
    loadingResources = true;
    statusMsg = 'Start all initiated';
    await API.startAll(resources);
    setTimeout(loadResources, 3000);
  }

  async function stopAll() {
    if (!resources.some(r => r.state === 'running')) {
      statusMsg = 'No running resources';
      return;
    }
    loadingResources = true;
    statusMsg = 'Stop all initiated';
    await API.stopAll(resources);
    setTimeout(loadResources, 3000);
  }

  loadResources();
</script>

<div class="nav-bar">
  <button class="back" on:click={onBack}><Icon name="arrow-left" size={15} />Back</button>
  <h1>{resourceFilter === 'running' ? 'Running Instances' : 'All Instances'}</h1>
  <span style="min-width: 60px"></span>
</div>
<div class="container">
  <div class="action-bar">
    <button on:click={loadResources} disabled={loadingResources}
      style="background: rgba(124,92,255,0.12); color: var(--primary-light)">
      <Icon name="refresh-cw" size={14} />Refresh
    </button>
    {#if resourceFilter === 'all'}
      <button on:click={startAll} disabled={loadingResources}
        style="background: rgba(52,211,153,0.14); color: var(--green)">
        <Icon name="play" size={14} />Start All
      </button>
    {/if}
    <button on:click={stopAll} disabled={loadingResources}
      style="background: rgba(251,113,133,0.14); color: var(--red)">
      <Icon name="stop-circle" size={14} />Stop All
    </button>
  </div>
  {#if loadingResources}
    <div class="center" style="padding: 16px" transition:fade={{ duration: 150 }}>
      <Icon name="loader" size={24} strokeWidth={2.5} class="spinner-icon" />
    </div>
  {/if}
  {#if statusMsg && !loadingResources}
    <div class="status-msg" transition:fade={{ duration: 150 }}>{statusMsg}</div>
  {/if}
  {#each resources as r (r.id + '-' + r.provider)}
    <div class="resource-row" animate:flip={{ duration: 250 }} transition:scale={{ duration: 200, start: 0.96 }}>
      <div class="header">
        <span class="name">{r.name}</span>
        <span class="badge state-{r.state}">{r.state}</span>
      </div>
      <div>
        <span class="badge provider-{r.provider}">{r.provider.toUpperCase()}</span>
        <span class="muted" style="margin-left: 8px">{r.instanceType}</span>
      </div>
      <div class="muted">{r.region}</div>
      {#if r.linkUrl}
        <div class="muted">
          <a href={r.linkUrl} target="_blank" rel="noopener">{r.linkUrl}</a>
        </div>
      {:else if r.publicIp}
        <div class="muted">{r.publicIp}</div>
      {/if}
      <div class="actions">
        <button class="action-start" disabled={r.state !== 'stopped'} on:click={() => startOne(r)}><Icon name="play" size={13} />Start</button>
        <button class="action-stop" disabled={r.state !== 'running'} on:click={() => stopOne(r)}><Icon name="stop-circle" size={13} />Stop</button>
      </div>
    </div>
  {/each}
</div>
