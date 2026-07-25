<script>
  import { fade, fly } from 'svelte/transition';

  let password = '';
  let errorMsg = '';
  let showError = false;
  let submitting = false;

  async function handleSubmit(e) {
    e.preventDefault();
    showError = false;
    submitting = true;

    try {
      const res = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ password }),
      });

      if (res.ok) {
        const params = new URLSearchParams(window.location.search);
        window.location.href = params.get('returnTo') || '/';
        return;
      }

      if (res.status === 401) {
        errorMsg = 'Incorrect passphrase.';
      } else if (res.status === 429) {
        errorMsg = 'Too many attempts. Try again in a minute.';
      } else {
        errorMsg = `Sign-in failed (HTTP ${res.status}).`;
      }
      showError = true;
    } catch (err) {
      errorMsg = `Network error: ${err.message}`;
      showError = true;
    } finally {
      submitting = false;
    }
  }
</script>

<div class="login-box" in:fly={{ y: 16, duration: 320 }}>
  <img src="/logo.svg" alt="" class="brand-hero" in:fade={{ delay: 120, duration: 300 }}>
  <h1 class="brand-text large center">Cloud Manager</h1>
  <p class="subtitle">Sign in to continue</p>
  <form on:submit={handleSubmit}>
    <div class="field">
      <label for="password">Passphrase</label>
      <input
        type="password"
        id="password"
        autocomplete="current-password"
        autofocus
        required
        bind:value={password}
      >
    </div>
    <button type="submit" class="primary" disabled={submitting}>
      {submitting ? 'Signing in…' : 'Sign In'}
    </button>
  </form>
  {#if showError}
    <div class="login-error" transition:fade={{ duration: 150 }}>{errorMsg}</div>
  {/if}
</div>
