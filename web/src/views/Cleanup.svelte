<script>
  import { fade, fly } from 'svelte/transition';
  import { API } from '../api.js';
  import Icon from '../icons/Icon.svelte';

  export let onBack;

  let cleanupStep = 0;
  let cloud = 'aws';
  let region = 'all-regions';
  let vpcId = '';
  let dryRun = false;
  let deleteText = '';
  let job = null;
  let pollHandle = null;
  let cleanupError = '';

  $: regionsForCloud = cloud === 'aws' ? API.awsRegions
                      : cloud === 'gcp' ? API.gcpRegions
                      : API.azureRegions;

  function setCloud(c) {
    cloud = c;
    const list = cloud === 'aws' ? API.awsRegions : cloud === 'gcp' ? API.gcpRegions : API.azureRegions;
    if (!list.includes(region)) region = list[0];
  }

  function isAllRegions() {
    return region === 'all-regions';
  }

  function resetCleanup() {
    if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
    cleanupStep = 0;
    cloud = 'aws';
    region = 'all-regions';
    vpcId = '';
    dryRun = false;
    deleteText = '';
    job = null;
    cleanupError = '';
  }

  function back() {
    resetCleanup();
    onBack();
  }

  function cleanupNext() { deleteText = ''; cleanupStep = 1; }
  function cleanupBack() { cleanupStep = Math.max(0, cleanupStep - 1); }

  $: canProceedCleanup = dryRun || deleteText.toUpperCase() === 'DELETE';

  async function cleanupConfirm() {
    cleanupStep = 2;
    cleanupError = '';
    job = null;
    try {
      const resp = await API.startCleanup(cloud, region, vpcId.trim() || null, dryRun);
      pollCleanup(resp.jobId);
    } catch (e) {
      cleanupError = `Failed to start: ${e.message}`;
    }
  }

  function pollCleanup(jobId) {
    const tick = async () => {
      try {
        const j = await API.getCleanupStatus(jobId);
        job = j;
        if (j.status === 'COMPLETE' || j.status === 'ERROR') {
          clearInterval(pollHandle);
          pollHandle = null;
        }
      } catch (e) {
        cleanupError = `Poll error: ${e.message}`;
      }
    };
    tick();
    pollHandle = setInterval(tick, 3000);
  }

  function cleanupDone() { cleanupStep = 3; }
  function cleanupRestart() { resetCleanup(); }

  // ── Single-region progress ────────────────────────────────────────────────
  function hasChildren() {
    return job?.children?.length > 0;
  }

  function progressPercent() {
    if (!job) return 0;
    if (hasChildren()) return multiProgressPercent();
    if (!job.steps.length) return 0;
    const total = job.steps[job.steps.length - 1]?.total || 27;
    const done = job.steps.filter(s => s.state === 'done' || s.state === 'skipped').length;
    return total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  }

  function progressDone() {
    if (!job) return 0;
    if (hasChildren()) return job.children.filter(c => c.status === 'COMPLETE' || c.status === 'ERROR').length;
    return job.steps.filter(s => s.state === 'done' || s.state === 'skipped').length;
  }

  function progressTotal() {
    if (hasChildren()) return job.children.length;
    return job?.steps[job.steps.length - 1]?.total || 27;
  }

  function progressLabel() {
    return hasChildren() ? 'regions' : 'steps';
  }

  // ── Multi-region helpers ──────────────────────────────────────────────────
  function multiProgressPercent() {
    if (!hasChildren()) return 0;
    const total = job.children.length;
    const done = job.children.filter(c => c.status === 'COMPLETE' || c.status === 'ERROR').length;
    return Math.round((done / total) * 100);
  }

  function childStepsDone(child) {
    return (child.steps || []).filter(s => s.state === 'done' || s.state === 'skipped').length;
  }

  function childStepsTotal(child) {
    const steps = child.steps || [];
    return steps[steps.length - 1]?.total || 27;
  }

  function childPercent(child) {
    const total = childStepsTotal(child);
    const done = childStepsDone(child);
    return total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  }

  function childIconName(status) {
    return ({ COMPLETE: 'check-circle', ERROR: 'x-circle', RUNNING: 'loader', PENDING: 'circle' })[status] || 'circle';
  }

  function childHasError(child) {
    return child.status === 'ERROR';
  }

  function statusTitle(s) {
    return ({ PENDING: 'Queued…', RUNNING: 'Running…', COMPLETE: 'Complete', ERROR: 'Error' })[s] || s;
  }

  function stepIconName(state) {
    return ({ done: 'check-circle', skipped: 'skip-forward', error: 'x-circle', running: 'loader' })[state] || 'circle';
  }
</script>

<div class="nav-bar">
  <button class="back" on:click={back}><Icon name="arrow-left" size={15} />Back</button>
  <h1>Cloud Cleanup</h1>
  <span style="min-width: 60px"></span>
</div>
<div class="container">

  {#if cleanupStep === 0}
    <div transition:fade={{ duration: 180 }}>
      <div class="center" style="margin: 16px 0; color: var(--red)"><Icon name="trash" size={44} strokeWidth={1.5} /></div>
      <h2 class="center" style="margin-bottom: 16px">Configure Cleanup</h2>

      <div class="card">
        <label>Cloud Provider</label>
        <div class="seg-row">
          <button class:active={cloud === 'aws'} on:click={() => setCloud('aws')}>AWS</button>
          <button class:active={cloud === 'azure'} on:click={() => setCloud('azure')}>Azure</button>
          <button class:active={cloud === 'gcp'} on:click={() => setCloud('gcp')}>GCP</button>
        </div>
      </div>

      <div class="card">
        <label for="region">Region</label>
        <select id="region" bind:value={region}>
          {#each regionsForCloud as r}
            <option value={r}>{r}</option>
          {/each}
        </select>
      </div>

      {#if !isAllRegions() && cloud !== 'gcp'}
        <div class="card" transition:fade={{ duration: 150 }}>
          <label>{cloud === 'aws' ? 'VPC ID (optional)' : 'Resource Group (optional)'}</label>
          <input type="text" bind:value={vpcId}
            placeholder={cloud === 'aws' ? 'vpc-xxxxxxxx' : 'my-resource-group'}>
          <p class="muted" style="margin-top: 6px">
            Leave blank to clean up all matched resources in the region.
          </p>
        </div>
      {/if}

      <div class="card">
        <div class="toggle-row">
          <span style="font-weight: 500; display: inline-flex; align-items: center; gap: 8px"><Icon name="eye" size={16} />Dry Run</span>
          <label class="switch">
            <input type="checkbox" bind:checked={dryRun}>
            <span class="slider"></span>
          </label>
        </div>
        <p class="muted" style="margin-top: 6px">
          Dry run shows what would be deleted without actually deleting anything.
        </p>
      </div>

      <button class="primary danger" on:click={cleanupNext} disabled={!region}>Next →</button>
    </div>
  {:else if cleanupStep === 1}
    <div transition:fly={{ x: 24, duration: 200 }}>
      <div class="center" style="margin: 16px 0; color: {dryRun ? 'var(--primary-light)' : 'var(--orange)'}">
        <Icon name={dryRun ? 'search' : 'alert-triangle'} size={44} strokeWidth={1.5} />
      </div>
      <h2 class="center" style="margin-bottom: 16px">{dryRun ? 'Dry Run Review' : 'Confirm Deletion'}</h2>

      <div class="card">
        <div class="summary-row"><span class="label">Cloud</span><span class="value">{cloud.toUpperCase()}</span></div>
        <div class="summary-row">
          <span class="label">Region</span>
          <span class="value">{isAllRegions() ? 'All Regions (parallel)' : region}</span>
        </div>
        {#if vpcId && !isAllRegions() && cloud !== 'gcp'}
          <div class="summary-row">
            <span class="label">{cloud === 'aws' ? 'VPC' : 'Resource Group'}</span>
            <span class="value">{vpcId}</span>
          </div>
        {/if}
        <div class="summary-row">
          <span class="label">Mode</span>
          <span class="value">{dryRun ? 'Dry Run (safe)' : 'LIVE DELETE'}</span>
        </div>
      </div>

      {#if !dryRun}
        <div class="card">
          <p style="color: var(--red); font-size: 14px; margin-bottom: 12px">
            This will permanently delete all matched cloud resources. This action cannot be undone.
          </p>
          <label for="deleteText">Type DELETE to confirm:</label>
          <input type="text" id="deleteText" bind:value={deleteText} placeholder="DELETE"
            style="text-transform: uppercase">
        </div>
      {/if}

      <div style="display: flex; gap: 12px;">
        <button class="primary secondary" style="flex: 1" on:click={cleanupBack}><Icon name="arrow-left" size={15} />Back</button>
        <button class="primary danger" style="flex: 1"
          disabled={!canProceedCleanup}
          on:click={cleanupConfirm}>
          {dryRun ? 'Start Dry Run' : 'Delete Now'}
        </button>
      </div>
    </div>
  {:else if cleanupStep === 2}
    <div transition:fade={{ duration: 180 }}>
      {#if !job && !cleanupError}
        <div class="center">
          <p style="margin-bottom: 12px">Starting job…</p>
          <Icon name="loader" size={24} strokeWidth={2.5} class="spinner-icon" />
        </div>
      {/if}

      {#if cleanupError}
        <div>
          <p class="error-msg">{cleanupError}</p>
          <button class="primary danger" style="margin-top: 16px" on:click={cleanupBack}><Icon name="arrow-left" size={15} />Back</button>
        </div>
      {/if}

      {#if job}
        <div>
          <h2 class="center">{statusTitle(job.status)}</h2>

          <div class="progress-bar" class:error={job.status === 'ERROR'}>
            <div class="fill" style="width: {progressPercent()}%"></div>
          </div>
          <p class="center muted">
            {progressDone()} / {progressTotal()} {progressLabel()}
          </p>

          {#if !job.children || !job.children.length}
            <div class="card" style="padding: 0; margin-top: 16px">
              {#each job.steps as s, idx (idx)}
                <div class="step-row">
                  <span class="step-icon" class:spinner-icon={s.state === 'running'} style="color: {s.state === 'error' ? 'var(--red)' : s.state === 'done' ? 'var(--green)' : 'var(--muted)'}">
                    <Icon name={stepIconName(s.state)} size={18} />
                  </span>
                  <div style="flex: 1; min-width: 0">
                    <div class="step-name">
                      <span>{s.number}/{s.total}</span>
                      <span>{s.name}</span>
                    </div>
                    {#if s.detail}
                      <div class="step-detail">{s.detail}</div>
                    {/if}
                  </div>
                </div>
              {/each}
            </div>
          {:else}
            <div style="margin-top: 16px">
              {#each job.children as child (child.jobId)}
                <div class="card" style="padding: 12px 14px; margin-bottom: 8px">
                  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 6px">
                    <span style="font-size:13px; font-weight:600">{child.region}</span>
                    <span
                      class:spinner-icon={child.status === 'RUNNING'}
                      style="color: {child.status === 'ERROR' ? 'var(--red)' : child.status === 'COMPLETE' ? 'var(--green)' : 'var(--muted)'}; display: inline-flex"
                    >
                      <Icon name={childIconName(child.status)} size={16} />
                    </span>
                  </div>
                  <div style="height:5px; background:var(--border); border-radius:3px; overflow:hidden">
                    <div style="width:{childPercent(child)}%; height:100%; background:{child.status==='ERROR'?'var(--red)':child.status==='COMPLETE'?'var(--green)':'var(--primary)'}; transition:width 0.3s"></div>
                  </div>
                  <div style="display:flex; justify-content:space-between; margin-top:4px">
                    <span class="muted">{child.status === 'PENDING' ? 'Queued…' : child.status === 'RUNNING' ? `${childStepsDone(child)}/${childStepsTotal(child)} steps` : child.status}</span>
                    {#if childHasError(child)}
                      <span class="muted" style="color:var(--red); font-size:11px; max-width:60%; text-align:right; white-space:nowrap; overflow:hidden; text-overflow:ellipsis">
                        {(child.steps || []).filter(s => s.state === 'error').map(s => s.name).join(', ')}
                      </span>
                    {/if}
                  </div>
                </div>
              {/each}
            </div>
          {/if}

          {#if job.status === 'COMPLETE' || job.status === 'ERROR'}
            <div style="margin-top: 16px">
              <button class="primary" on:click={cleanupDone}>Done</button>
            </div>
          {:else}
            <div class="center" style="margin-top: 16px; color: var(--primary-light)">
              <Icon name="loader" size={24} strokeWidth={2.5} class="spinner-icon" />
            </div>
          {/if}
        </div>
      {/if}
    </div>
  {:else if cleanupStep === 3}
    <div class="center" style="margin-top: 32px" transition:fly={{ y: 12, duration: 220 }}>
      <div style="margin-bottom: 16px; color: var(--green)"><Icon name="check-circle" size={64} strokeWidth={1.5} /></div>
      <h2>{dryRun ? 'Dry Run Complete' : 'Cleanup Complete'}</h2>
      <p class="muted" style="margin: 12px 0; padding: 0 20px">
        {isAllRegions()
          ? 'All regions processed. Check above for any per-region errors.'
          : (dryRun ? 'No resources were deleted. Review the steps above.' : 'All matched resources have been deleted.')}
      </p>
      <button class="primary" on:click={cleanupRestart} style="margin-top: 16px">Start New Cleanup</button>
    </div>
  {/if}

</div>
