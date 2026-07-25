<script>
  import Icon from '../icons/Icon.svelte';

  export let openResources;
  export let openCleanup;
  export let openSchedule;
  export let logout;
</script>

<div class="nav-bar">
  <span style="min-width: 60px"></span>
  <h1>
    <span class="brand">
      <img src="/logo.svg" alt="" class="brand-icon">
      <span class="brand-text">Cloud Manager</span>
    </span>
  </h1>
  <button class="right" on:click={logout}><Icon name="log-out" size={16} />Logout</button>
</div>
<div class="container">
  <div class="welcome">Welcome 👋</div>

  <button class="dashboard-card" style="--accent: linear-gradient(135deg, #7c5cff, #a78bfa)" on:click={() => openResources('all')}>
    <div class="icon-wrap" style="background: linear-gradient(135deg, #7c5cff, #a78bfa)">
      <Icon name="list" size={24} />
    </div>
    <div>
      <div class="title">All Instances</div>
      <div class="desc">View all resources across all providers</div>
    </div>
  </button>

  <button class="dashboard-card" on:click={() => openResources('running')}>
    <div class="icon-wrap" style="background: linear-gradient(135deg, #34d399, #10b981)">
      <Icon name="play" size={22} />
    </div>
    <div>
      <div class="title">Running Instances</div>
      <div class="desc">View only currently running instances</div>
    </div>
  </button>

  <button class="dashboard-card" on:click={openCleanup}>
    <div class="icon-wrap" style="background: linear-gradient(135deg, #fb7185, #f43f5e)">
      <Icon name="trash" size={22} />
    </div>
    <div>
      <div class="title">Cloud Cleanup</div>
      <div class="desc">Delete VPC resources (AWS / Azure / GCP)</div>
    </div>
  </button>

  <button class="dashboard-card" on:click={openSchedule}>
    <div class="icon-wrap" style="background: linear-gradient(135deg, #2dd4bf, #06b6d4)">
      <Icon name="clock" size={22} />
    </div>
    <div>
      <div class="title">Schedule</div>
      <div class="desc">Auto-stop all running instances daily at a chosen time</div>
    </div>
  </button>
</div>
