(() => {
  let csrfToken = null;
  let authGeneration = 0;

  async function request(path, options = {}) {
    const requestGeneration = authGeneration;
    const headers = new Headers(options.headers || {});
    const method = (options.method || 'GET').toUpperCase();
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && csrfToken) {
      headers.set('X-OMP-Deck-CSRF', csrfToken);
    }
    const response = await fetch(path, {...options, headers, credentials: 'same-origin'});
    if (
      requestGeneration === authGeneration
      && (response.status === 401 || (response.status === 403 && path !== '/api/auth/pair'))
    ) {
      csrfToken = null;
      authGeneration += 1;
      window.dispatchEvent(new CustomEvent('omp-deck-auth-required'));
    }
    return response;
  }

  async function session() {
    const requestGeneration = authGeneration;
    const response = await fetch('/api/auth/session', {cache: 'no-store', credentials: 'same-origin'});
    if (!response.ok) throw new Error(`Authentication returned ${response.status}`);
    const payload = await response.json();
    if (requestGeneration === authGeneration) {
      authGeneration += 1;
      csrfToken = payload.authenticated ? payload.csrfToken : null;
    }
    return payload;
  }

  async function pair(token) {
    const response = await fetch('/api/auth/pair', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token}),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `Pairing returned ${response.status}`);
    authGeneration += 1;
    csrfToken = payload.csrfToken;
    return payload;
  }

  async function logout({all = false} = {}) {
    const response = await request(all ? '/api/auth/revoke-all' : '/api/auth/logout', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}',
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || `Logout returned ${response.status}`);
    }
    authGeneration += 1;
    csrfToken = null;
  }

  function shouldReuseRequestId(response, payload = {}) {
    if (
      payload.status === 'uncertain'
      || payload.errorCode === 'outcome_unknown'
    ) return true;
    if (['failed', 'cancelled', 'stale'].includes(payload.status)) return false;
    return response.ok || response.status >= 500;
  }

  window.ompDeckApi = {request, session, pair, logout, shouldReuseRequestId};
})();
