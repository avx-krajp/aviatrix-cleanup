function app() {
  return {
    view: 'home',

    resourceFilter: 'all',
    resources: [],
    loadingResources: false,
    statusMsg: '',

    cleanupStep: 0,
    cloud: 'aws',
    region: 'all-regions',
    vpcId: '',
    dryRun: false,
    deleteText: '',
    job: null,
    pollHandle: null,
    cleanupError: '',

    schedule: null,
    scheduleTime: '',
    scheduleSaving: false,
    scheduleStatusMsg: '',

    async logout() { await API.logout(); },

    get regionsForCloud() {
      if (this.cloud === 'aws')   return API.awsRegions;
      if (this.cloud === 'gcp')   return API.gcpRegions;
      return API.azureRegions;
    },

    setCloud(c) {
      this.cloud = c;
      const list = this.regionsForCloud;
      if (!list.includes(this.region)) this.region = list[0];
    },

    async openResources(filter) {
      this.view = 'resources';
      this.resourceFilter = filter;
      this.resources = [];
      this.statusMsg = '';
      await this.loadResources();
    },

    async loadResources() {
      this.loadingResources = true;
      this.statusMsg = '';
      const all = await API.fetchAllInstances();
      this.resources = this.resourceFilter === 'running'
        ? all.filter(r => r.state === 'running')
        : all;
      this.statusMsg = this.resources.length
        ? `Found ${this.resources.length} resource(s)`
        : 'No resources found';
      this.loadingResources = false;
    },

    async startOne(r) {
      const prevState = r.state;
      r.state = 'starting';
      this.statusMsg = `Starting ${r.name}...`;
      const ok = await API.startInstance(r);
      if (!ok) {
        r.state = prevState;
        this.statusMsg = `Failed to start ${r.name}`;
        return;
      }
      setTimeout(() => this.loadResources(), 3000);
    },

    async stopOne(r) {
      const prevState = r.state;
      r.state = 'stopping';
      this.statusMsg = `Stopping ${r.name}...`;
      const ok = await API.stopInstance(r);
      if (!ok) {
        r.state = prevState;
        this.statusMsg = `Failed to stop ${r.name}`;
        return;
      }
      setTimeout(() => this.loadResources(), 3000);
    },

    async startAll() {
      if (!this.resources.some(r => r.state === 'stopped')) {
        this.statusMsg = 'No stopped resources';
        return;
      }
      this.loadingResources = true;
      this.statusMsg = '▶️ Start all initiated';
      await API.startAll(this.resources);
      setTimeout(() => this.loadResources(), 3000);
    },

    async stopAll() {
      if (!this.resources.some(r => r.state === 'running')) {
        this.statusMsg = 'No running resources';
        return;
      }
      this.loadingResources = true;
      this.statusMsg = '⏹ Stop all initiated';
      await API.stopAll(this.resources);
      setTimeout(() => this.loadResources(), 3000);
    },

    openCleanup() {
      this.view = 'cleanup';
      this._resetCleanup();
    },

    isAllRegions() {
      return this.region === 'all-regions';
    },

    _resetCleanup() {
      if (this.pollHandle) { clearInterval(this.pollHandle); this.pollHandle = null; }
      this.cleanupStep = 0;
      this.cloud = 'aws';
      this.region = 'all-regions';
      this.vpcId = '';
      this.dryRun = false;
      this.deleteText = '';
      this.job = null;
      this.cleanupError = '';
    },

    cleanupNext() { this.deleteText = ''; this.cleanupStep = 1; },
    cleanupBack() { this.cleanupStep = Math.max(0, this.cleanupStep - 1); },

    canProceedCleanup() {
      return this.dryRun || this.deleteText.toUpperCase() === 'DELETE';
    },

    async cleanupConfirm() {
      this.cleanupStep = 2;
      this.cleanupError = '';
      this.job = null;
      try {
        const resp = await API.startCleanup(
          this.cloud, this.region, this.vpcId.trim() || null, this.dryRun
        );
        this._pollCleanup(resp.jobId);
      } catch (e) {
        this.cleanupError = `Failed to start: ${e.message}`;
      }
    },

    _pollCleanup(jobId) {
      const tick = async () => {
        try {
          const j = await API.getCleanupStatus(jobId);
          this.job = j;
          if (j.status === 'COMPLETE' || j.status === 'ERROR') {
            clearInterval(this.pollHandle);
            this.pollHandle = null;
          }
        } catch (e) {
          this.cleanupError = `Poll error: ${e.message}`;
        }
      };
      tick();
      this.pollHandle = setInterval(tick, 3000);
    },

    cleanupDone() { this.cleanupStep = 3; },
    cleanupRestart() { this._resetCleanup(); },

    async openSchedule() {
      this.view = 'schedule';
      this.schedule = null;
      this.scheduleStatusMsg = '';
      await this._loadSchedule();
      this.scheduleTime = this.schedule?.time || '19:00';
    },

    async _loadSchedule() {
      try {
        this.schedule = await API.getSchedule();
      } catch (e) {
        this.scheduleStatusMsg = `Failed to load: ${e.message}`;
      }
    },

    async saveSchedule() {
      if (!this.scheduleTime) return;
      this.scheduleSaving = true;
      this.scheduleStatusMsg = '';
      try {
        await API.putSchedule(this.scheduleTime);
        await this._loadSchedule();
        this.scheduleStatusMsg = `Saved: daily at ${this.schedule.time} IST`;
      } catch (e) {
        this.scheduleStatusMsg = `Failed: ${e.message}`;
      } finally {
        this.scheduleSaving = false;
      }
    },

    async toggleSkipToday() {
      this.scheduleSaving = true;
      this.scheduleStatusMsg = '';
      try {
        if (this.schedule?.isSkippedToday) {
          await API.unskipSchedule();
          this.scheduleStatusMsg = "Today's run restored";
        } else {
          await API.skipSchedule();
          this.scheduleStatusMsg = "Today's run will be skipped";
        }
        await this._loadSchedule();
      } catch (e) {
        this.scheduleStatusMsg = `Failed: ${e.message}`;
      } finally {
        this.scheduleSaving = false;
      }
    },

    // ── Single-region progress ────────────────────────────────────────────────
    _hasChildren() {
      return this.job?.children?.length > 0;
    },

    progressPercent() {
      if (!this.job) return 0;
      if (this._hasChildren()) return this.multiProgressPercent();
      if (!this.job.steps.length) return 0;
      const total = this.job.steps[this.job.steps.length - 1]?.total || 27;
      const done = this.job.steps.filter(s => s.state === 'done' || s.state === 'skipped').length;
      return total ? Math.min(100, Math.round((done / total) * 100)) : 0;
    },

    progressDone() {
      if (!this.job) return 0;
      if (this._hasChildren()) return this.job.children.filter(c => c.status === 'COMPLETE' || c.status === 'ERROR').length;
      return this.job.steps.filter(s => s.state === 'done' || s.state === 'skipped').length;
    },

    progressTotal() {
      if (this._hasChildren()) return this.job.children.length;
      return this.job?.steps[this.job.steps.length - 1]?.total || 27;
    },

    progressLabel() {
      if (this._hasChildren()) return 'regions';
      return 'steps';
    },

    // ── Multi-region helpers ──────────────────────────────────────────────────
    multiProgressPercent() {
      if (!this._hasChildren()) return 0;
      const total = this.job.children.length;
      const done = this.job.children.filter(c => c.status === 'COMPLETE' || c.status === 'ERROR').length;
      return Math.round((done / total) * 100);
    },

    childStepsDone(child) {
      return (child.steps || []).filter(s => s.state === 'done' || s.state === 'skipped').length;
    },

    childStepsTotal(child) {
      const steps = child.steps || [];
      return steps[steps.length - 1]?.total || 27;
    },

    childPercent(child) {
      const total = this.childStepsTotal(child);
      const done  = this.childStepsDone(child);
      return total ? Math.min(100, Math.round((done / total) * 100)) : 0;
    },

    childIcon(status) {
      return ({ COMPLETE: '✅', ERROR: '❌', RUNNING: '⏳', PENDING: '•' })[status] || '•';
    },

    childHasError(child) {
      return child.status === 'ERROR';
    },

    statusTitle(s) {
      return ({ PENDING: 'Queued…', RUNNING: 'Running…', COMPLETE: 'Complete', ERROR: 'Error' })[s] || s;
    },

    stepIcon(state) {
      return ({ done: '✅', skipped: '⏭️', error: '❌', running: '⏳' })[state] || '•';
    },
  };
}
