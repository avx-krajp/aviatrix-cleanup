<script>
  import { fade } from 'svelte/transition';
  import { API } from '../api.js';
  import Icon from '../icons/Icon.svelte';

  export let onBack;

  let schedule = null;
  let scheduleTime = '';
  let scheduleSaving = false;
  let scheduleStatusMsg = '';

  async function loadSchedule() {
    try {
      schedule = await API.getSchedule();
    } catch (e) {
      scheduleStatusMsg = `Failed to load: ${e.message}`;
    }
  }

  async function init() {
    await loadSchedule();
    scheduleTime = schedule?.time || '19:00';
  }
  init();

  async function saveSchedule() {
    if (!scheduleTime) return;
    scheduleSaving = true;
    scheduleStatusMsg = '';
    try {
      await API.putSchedule(scheduleTime);
      await loadSchedule();
      scheduleStatusMsg = `Saved: daily at ${schedule.time} IST`;
    } catch (e) {
      scheduleStatusMsg = `Failed: ${e.message}`;
    } finally {
      scheduleSaving = false;
    }
  }

  async function toggleSkipToday() {
    scheduleSaving = true;
    scheduleStatusMsg = '';
    try {
      if (schedule?.isSkippedToday) {
        await API.unskipSchedule();
        scheduleStatusMsg = "Today's run restored";
      } else {
        await API.skipSchedule();
        scheduleStatusMsg = "Today's run will be skipped";
      }
      await loadSchedule();
    } catch (e) {
      scheduleStatusMsg = `Failed: ${e.message}`;
    } finally {
      scheduleSaving = false;
    }
  }
</script>

<div class="nav-bar">
  <button class="back" on:click={onBack}><Icon name="arrow-left" size={15} />Back</button>
  <h1>Schedule</h1>
  <span style="min-width: 60px"></span>
</div>
<div class="container">

  <div class="card">
    <div class="summary-row">
      <span class="label">Current schedule</span>
      <span class="value">{schedule?.time ? `Daily at ${schedule.time} IST` : 'Not set'}</span>
    </div>
    {#if schedule?.time}
      <div class="summary-row">
        <span class="label">Today's run</span>
        <span class="value">{schedule?.isSkippedToday ? 'Skipped' : `Will run at ${schedule?.time} IST`}</span>
      </div>
    {/if}
  </div>

  <div class="card">
    <label for="scheduleTime">Stop-all time (IST)</label>
    <input type="time" id="scheduleTime" bind:value={scheduleTime} step="60">
    <p class="muted" style="margin-top: 6px">
      All running instances across AWS, Azure and GCP will be stopped at this time every day.
    </p>
  </div>

  <button class="primary" disabled={!scheduleTime || scheduleSaving} on:click={saveSchedule}>
    {scheduleSaving ? 'Saving…' : (schedule?.time ? 'Update schedule' : 'Save schedule')}
  </button>

  {#if schedule?.time}
    <button class="primary secondary" style="margin-top: 8px" disabled={scheduleSaving} on:click={toggleSkipToday}>
      {schedule?.isSkippedToday ? 'Undo skip — run today' : "Cancel today's run"}
    </button>
  {/if}

  {#if scheduleStatusMsg}
    <p class="muted center" style="margin-top: 12px" transition:fade={{ duration: 150 }}>{scheduleStatusMsg}</p>
  {/if}
</div>
