// API base: relative paths in production (served behind CloudFront).
// All /api/* calls go through CloudFront → API Gateway, with auth cookie
// sent automatically via credentials: 'include'.
const API_BASE = '';

const API = {
  awsRegions: [
    'all-regions',
    'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2',
    'ap-south-1', 'ap-northeast-1', 'ap-northeast-2', 'ap-northeast-3',
    'ap-southeast-1', 'ap-southeast-2',
    'ca-central-1',
    'eu-central-1', 'eu-west-1', 'eu-west-2', 'eu-west-3', 'eu-north-1',
    'sa-east-1',
  ],

  gcpRegions: [
    'all-regions',
    'us-central1', 'us-east1', 'us-east4', 'us-east5',
    'us-south1', 'us-west1', 'us-west2', 'us-west3', 'us-west4',
    'northamerica-northeast1', 'northamerica-northeast2',
    'southamerica-east1', 'southamerica-west1',
    'europe-west1', 'europe-west2', 'europe-west3', 'europe-west4',
    'europe-west6', 'europe-west8', 'europe-west9', 'europe-west10', 'europe-west12',
    'europe-north1', 'europe-central2', 'europe-southwest1',
    'asia-east1', 'asia-east2',
    'asia-northeast1', 'asia-northeast2', 'asia-northeast3',
    'asia-south1', 'asia-south2',
    'asia-southeast1', 'asia-southeast2',
    'australia-southeast1', 'australia-southeast2',
    'me-west1', 'me-central1', 'me-central2',
    'africa-south1',
  ],

  azureRegions: [
    'all-regions',
    'eastus', 'eastus2', 'centralus', 'northcentralus', 'southcentralus',
    'westus', 'westus2', 'westus3', 'westcentralus',
    'canadacentral', 'canadaeast',
    'brazilsouth', 'brazilsoutheast', 'mexicocentral',
    'northeurope', 'westeurope', 'uksouth', 'ukwest',
    'francecentral', 'francesouth',
    'germanywestcentral', 'germanynorth',
    'norwayeast', 'norwaywest', 'swedencentral',
    'switzerlandnorth', 'switzerlandwest',
    'italynorth', 'polandcentral', 'spaincentral',
    'eastasia', 'southeastasia', 'japaneast', 'japanwest',
    'koreacentral', 'koreasouth',
    'australiaeast', 'australiasoutheast', 'australiacentral',
    'centralindia', 'southindia', 'westindia',
    'uaenorth', 'uaecentral', 'qatarcentral', 'israelcentral',
    'southafricanorth', 'southafricawest',
  ],

  async _fetch(url, opts = {}) {
    const res = await fetch(url, {
      ...opts,
      credentials: 'include',
    });
    if (res.status === 401 && url.startsWith(API_BASE)) {
      const here = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.href = `/login.html?returnTo=${here}`;
      throw new Error('Unauthenticated');
    }
    return res;
  },

  async logout() {
    try { await this._fetch(`${API_BASE}/api/logout`, { method: 'POST' }); }
    catch {}
    window.location.href = '/login.html';
  },

  async startCleanup(cloud, region, vpcId, dryRun) {
    const body = { cloud, region, dry_run: dryRun };
    if (vpcId) body.vpc_id = vpcId;
    const res = await this._fetch(`${API_BASE}/api/cleanup`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    return res.json();
  },

  async getCleanupStatus(jobId) {
    const res = await this._fetch(
      `${API_BASE}/api/cleanup/status?jobId=${encodeURIComponent(jobId)}`
    );
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    return res.json();
  },

  async getSchedule() {
    const res = await this._fetch(`${API_BASE}/api/schedule`);
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    return res.json();
  },

  async putSchedule(time) {
    const res = await this._fetch(`${API_BASE}/api/schedule`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ time }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    return res.json();
  },

  async skipSchedule() {
    const res = await this._fetch(`${API_BASE}/api/schedule/skip`, { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    return res.json();
  },

  async unskipSchedule() {
    const res = await this._fetch(`${API_BASE}/api/schedule/unskip`, { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    return res.json();
  },

  async fetchInstances(provider, region) {
    const params = new URLSearchParams({ provider });
    if (region) params.set(provider === 'gcp' ? 'zone' : 'region', region);
    try {
      const res = await this._fetch(`${API_BASE}/api/instances?${params}`);
      if (!res.ok) return [];
      const data = await res.json();
      if (data.status !== 'success') return [];
      return (data.instances || []).map(item => ({
        id: item.id,
        name: item.name,
        state: this._mapState(item.state, provider),
        provider,
        region: item.region || this._defaultRegion(provider),
        instanceType: item.instanceType || 'unknown',
        resourceGroup: item.resourceGroup || '',
        publicIp: item.publicIp || '',
      }));
    } catch {
      return [];
    }
  },

  async fetchAllInstances() {
    const [aws, gcp, azure] = await Promise.all([
      this.fetchInstances('aws'),
      this.fetchInstances('gcp'),
      this.fetchInstances('azure'),
    ]);
    return [...aws, ...gcp, ...azure];
  },

  async startInstance(r) {
    const params = new URLSearchParams({ provider: r.provider });
    switch (r.provider) {
      case 'aws':   params.set('instanceId', r.id); params.set('region', r.region); break;
      case 'gcp':   params.set('vmName', r.name); params.set('zone', r.region); break;
      case 'azure': params.set('vmName', r.name); params.set('resourceGroup', r.resourceGroup); params.set('region', r.region); break;
    }
    return this._call(`${API_BASE}/api/instances/start?${params}`);
  },

  async stopInstance(r) {
    const params = new URLSearchParams({ provider: r.provider });
    switch (r.provider) {
      case 'aws':   params.set('instanceId', r.id); params.set('region', r.region); break;
      case 'gcp':   params.set('vmName', r.name); params.set('zone', r.region); break;
      case 'azure': params.set('vmName', r.name); params.set('resourceGroup', r.resourceGroup); params.set('region', r.region); break;
    }
    return this._call(`${API_BASE}/api/instances/stop?${params}`);
  },

  async startAll(resources) {
    const stopped = resources.filter(r => r.state === 'stopped');
    const awsByRegion = {};
    for (const r of stopped) {
      if (r.provider === 'aws') (awsByRegion[r.region] ??= []).push(r);
    }
    const tasks = [];
    for (const [region, group] of Object.entries(awsByRegion)) {
      const ids = group.map(r => r.id).join(',');
      const params = new URLSearchParams({ provider: 'aws', instanceIds: ids, region });
      tasks.push(this._call(`${API_BASE}/api/instances/start-all?${params}`));
    }
    for (const r of stopped.filter(r => r.provider !== 'aws')) {
      tasks.push(this.startInstance(r));
    }
    await Promise.all(tasks);
  },

  async stopAll(resources) {
    const running = resources.filter(r => r.state === 'running');
    const awsByRegion = {};
    for (const r of running) {
      if (r.provider === 'aws') (awsByRegion[r.region] ??= []).push(r);
    }
    const tasks = [];
    for (const [region, group] of Object.entries(awsByRegion)) {
      const ids = group.map(r => r.id).join(',');
      const params = new URLSearchParams({ provider: 'aws', instanceIds: ids, region });
      tasks.push(this._call(`${API_BASE}/api/instances/stop-all?${params}`));
    }
    for (const r of running.filter(r => r.provider !== 'aws')) {
      tasks.push(this.stopInstance(r));
    }
    await Promise.all(tasks);
  },

  async _call(url) {
    try {
      const res = await this._fetch(url, { method: 'POST' });
      if (!res.ok) return false;
      const data = await res.json();
      return data.status === 'success';
    } catch {
      return false;
    }
  },

  _mapState(state, provider) {
    const s = (state || '').toLowerCase();
    if (s === 'running') return 'running';
    if (['stopped', 'deallocated', 'terminated'].includes(s)) return 'stopped';
    if (s === 'stopping') return 'stopping';
    if (['starting', 'pending', 'provisioning', 'staging'].includes(s)) return 'starting';
    return 'unknown';
  },

  _defaultRegion(provider) {
    return provider === 'aws' ? 'us-east-1'
         : provider === 'gcp' ? 'us-central1-a'
         : 'eastus';
  },
};
