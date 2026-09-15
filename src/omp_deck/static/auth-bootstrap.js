(() => {
  'use strict';

  function createAuthRecovery({api, showApplication, resetApplication, clearLocalData}) {
    const screen = document.getElementById('auth-screen');
    const app = document.getElementById('app');
    const form = document.getElementById('pair-form');
    const fields = document.getElementById('pairing-fields');
    const token = document.getElementById('pair-token');
    const error = document.getElementById('auth-error');
    const status = document.getElementById('auth-status');
    const title = document.getElementById('auth-title');
    const description = document.getElementById('auth-description');
    const submit = document.getElementById('pair-submit');
    const retry = document.getElementById('auth-retry');
    let checkController = null;
    let generation = 0;

    const begin = () => {
      generation += 1;
      checkController?.abort();
      checkController = new AbortController();
      return {generation, signal: checkController.signal};
    };

    const showScreen = () => {
      app.hidden = true;
      screen.hidden = false;
    };

    const showChecking = () => {
      showScreen();
      fields.hidden = true;
      retry.hidden = true;
      error.hidden = true;
      error.textContent = '';
      title.textContent = 'Connecting to OMP Deck';
      description.textContent = 'Checking this device’s secure session…';
      status.textContent = 'Checking connection…';
    };

    const showRetry = message => {
      showScreen();
      fields.hidden = true;
      retry.hidden = false;
      title.textContent = 'OMP Deck is unreachable';
      description.textContent = 'Pairing status is unchanged. Restore the connection, then retry.';
      status.textContent = '';
      error.textContent = message || 'Unable to reach OMP Deck.';
      error.hidden = false;
      retry.focus();
    };

    const showUnpaired = ({clearToken = false} = {}) => {
      showScreen();
      fields.hidden = false;
      submit.disabled = false;
      retry.hidden = true;
      error.hidden = true;
      error.textContent = '';
      status.textContent = '';
      title.textContent = 'Pair this device';
      description.textContent = 'Enter the one-time code shown by the command below.';
      document.getElementById('pair-command').textContent = api.mode() === 'coordinator'
        ? 'omp-deck coordinator pair --config FILE'
        : 'omp-deck pair';
      if (clearToken) token.value = '';
      token.focus();
    };

    const check = async () => {
      const request = begin();
      showChecking();
      try {
        const session = await api.session({signal: request.signal});
        if (request.generation !== generation) return;
        checkController = null;
        if (session.authenticated) await showApplication();
        else {
          resetApplication();
          showUnpaired({clearToken: true});
        }
      } catch (caught) {
        if (request.signal.aborted || request.generation !== generation) return;
        checkController?.abort();
        checkController = null;
        showRetry(caught.message);
      }
    };

    retry.addEventListener('click', () => { void check(); });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const request = begin();
      error.hidden = true;
      status.textContent = 'Pairing…';
      submit.disabled = true;
      try {
        await api.pair(token.value.trim(), {signal: request.signal});
        status.textContent = 'Paired. Verifying secure session…';
        const session = await api.session({signal: request.signal});
        if (request.generation !== generation) return;
        if (!session.authenticated) {
          throw new Error('Pairing succeeded, but the secure session was not retained. Open OMP Deck over HTTPS.');
        }
        if (api.mode() === 'coordinator') {
          status.textContent = 'Paired. Reloading the authenticated dashboard…';
          location.reload();
          return;
        }
        checkController = null;
        await showApplication();
      } catch (caught) {
        if (request.signal.aborted || request.generation !== generation) return;
        status.textContent = '';
        error.textContent = caught.message;
        error.hidden = false;
        token.select();
      } finally {
        if (request.generation === generation) submit.disabled = false;
      }
    });

    const retryIfDisconnected = () => {
      if (!retry.hidden || (checkController && fields.hidden)) void check();
    };
    window.addEventListener('online', retryIfDisconnected);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') retryIfDisconnected();
    });

    window.addEventListener('omp-deck-auth-required', () => {
      begin();
      void clearLocalData();
      resetApplication();
      showUnpaired();
    });
    document.addEventListener('DOMContentLoaded', () => { void check(); }, {once: true});

    return Object.freeze({
      check,
      requirePairing(options) {
        begin();
        resetApplication();
        showUnpaired(options);
      },
    });
  }

  window.createAuthRecovery = createAuthRecovery;
})();
