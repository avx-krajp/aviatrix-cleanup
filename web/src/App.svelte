<script>
  import { fly, fade } from 'svelte/transition';
  import { API } from './api.js';
  import Home from './views/Home.svelte';
  import Resources from './views/Resources.svelte';
  import Cleanup from './views/Cleanup.svelte';
  import Schedule from './views/Schedule.svelte';

  let view = 'home';
  let resourceFilter = 'all';

  function openResources(filter) {
    resourceFilter = filter;
    view = 'resources';
  }
  function openCleanup() { view = 'cleanup'; }
  function openSchedule() { view = 'schedule'; }
  function goHome() { view = 'home'; }
  function logout() { API.logout(); }
</script>

{#if view === 'home'}
  <div in:fade={{ duration: 200 }}>
    <Home {openResources} {openCleanup} {openSchedule} {logout} />
  </div>
{:else if view === 'resources'}
  <div in:fly={{ x: 24, duration: 220 }} out:fade={{ duration: 120 }}>
    <Resources {resourceFilter} onBack={goHome} />
  </div>
{:else if view === 'cleanup'}
  <div in:fly={{ x: 24, duration: 220 }} out:fade={{ duration: 120 }}>
    <Cleanup onBack={goHome} />
  </div>
{:else if view === 'schedule'}
  <div in:fly={{ x: 24, duration: 220 }} out:fade={{ duration: 120 }}>
    <Schedule onBack={goHome} />
  </div>
{/if}
