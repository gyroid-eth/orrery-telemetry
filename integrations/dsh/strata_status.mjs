// Ordinary busy/timeouts can affect the shared live server. No polling or lease.
export async function assertIdle(fetchImpl, baseURL, key, audit, timeoutMs = 5000) {
  try {
    for (const endpoint of ['/v1/status', '/health']) {
      const url = new URL(endpoint, baseURL);
      let response = await fetchImpl(url, {redirect: 'manual', signal: AbortSignal.timeout(timeoutMs)});
      if (response.status === 401 || response.status === 403) {
        if (!key) throw new Error('STATUS_AUTH_REQUIRED');
        response = await fetchImpl(url, {redirect: 'manual',
          headers: {Authorization: 'Bearer ' + key}, signal: AbortSignal.timeout(timeoutMs)});
      }
      if (response.status >= 300 && response.status < 400) {
        audit({phase: 'blocked-redirect', from_origin: url.origin});
        throw new Error('STATUS_REDIRECT');
      }
      if (response.status === 404) continue;
      if (!response.ok) throw new Error('STATUS_UNAVAILABLE');
      const value = await response.json();
      const n = value.activity?.in_flight ?? value.in_flight;
      const q = value.activity?.queued ?? value.activity?.queue ?? value.queued ?? value.queue;
      const known = typeof n === 'number' && (q === undefined || typeof q === 'number');
      const busy = known ? n !== 0 || (q !== undefined && q !== 0) : null;
      audit({phase: 'status-check', busy});
      if (busy !== false) throw new Error('STATUS_NOT_IDLE');
      return;
    }
    throw new Error('STATUS_MISSING');
  } catch (error) {
    if (error.message !== 'STATUS_NOT_IDLE') audit({phase: 'status-check', busy: null});
    throw new Error('DSH_STATUS_UNAVAILABLE');
  }
}
