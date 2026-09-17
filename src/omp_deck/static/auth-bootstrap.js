(() => {
  'use strict';

  function createAuthRecovery({api, showApplication, resetApplication}) {
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
    const tailscaleSignIn = document.getElementById('tailscale-sign-in');
    const retry = document.getElementById('auth-retry');
    let checkController = null;
    let generation = 0;

    const begin = () => {
      generation += 1;
      checkController?.abort();
      checkController = new AbortController();
      return {
        generation,
        signal: checkController.signal,
        focusedElement: document.activeElement,
      };
    };

    const isCurrent = request => (
      !request.signal.aborted && request.generation === generation
    );

    const focusUnchanged = request => {
      const active = document.activeElement;
      return !active
        || active === document.body
        || active === document.documentElement
        || active === request?.focusedElement
        || !active.isConnected
        || active.closest('[hidden]');
    };

    const showScreen = () => {
      app.hidden = true;
      screen.hidden = false;
    };

    const hideActions = () => {
      fields.hidden = true;
      tailscaleSignIn.hidden = true;
      retry.hidden = true;
    };

    const clearMessage = () => {
      error.hidden = true;
      error.textContent = '';
      status.textContent = '';
    };

    const showChecking = () => {
      showScreen();
      hideActions();
      clearMessage();
      title.textContent = 'Connecting to OMP Deck';
      description.textContent = 'Checking this device’s secure session…';
      status.textContent = 'Checking connection…';
    };

    const showRetry = (message, request) => {
      showScreen();
      hideActions();
      retry.hidden = false;
      title.textContent = 'OMP Deck is unreachable';
      description.textContent = 'Authentication status is unchanged. Restore the connection, then retry.';
      status.textContent = '';
      error.textContent = message || 'Unable to reach OMP Deck.';
      error.hidden = false;
      if (focusUnchanged(request)) retry.focus();
    };

    const showPairing = (options = {}, request) => {
      const {clearToken = false} = options;
      showScreen();
      hideActions();
      clearMessage();
      fields.hidden = false;
      submit.disabled = false;
      title.textContent = 'Pair this device';
      description.textContent = 'Enter the one-time code shown by the command below.';
      document.getElementById('pair-command').textContent = api.mode() === 'coordinator'
        ? 'omp-deck coordinator pair --config FILE'
        : 'omp-deck pair';
      if (clearToken) token.value = '';
      if (focusUnchanged(request)) token.focus();
    };

    const showTailscale = (options = {}, request) => {
      const {signedOut = false, retentionFailed = false, message = ''} = options;
      showScreen();
      hideActions();
      clearMessage();
      tailscaleSignIn.hidden = false;
      tailscaleSignIn.disabled = false;
      if (signedOut) {
        title.textContent = 'Signed out of OMP Deck';
        description.textContent = 'Sign in again with the approved Tailscale account to continue.';
      } else if (retentionFailed) {
        title.textContent = 'Tailscale sign-in did not persist';
        description.textContent = 'This browser did not retain the secure OMP Deck session.';
      } else {
        title.textContent = 'Tailscale access denied';
        description.textContent = 'OMP Deck could not verify an approved Tailscale account for this connection.';
      }
      if (message) {
        error.textContent = message;
        error.hidden = false;
      }
      if (focusUnchanged(request)) tailscaleSignIn.focus();
    };

    const showTailscaleChecking = () => {
      showScreen();
      hideActions();
      clearMessage();
      title.textContent = 'Signing in with Tailscale';
      description.textContent = 'Verifying the approved Tailscale account for this connection…';
      status.textContent = 'Signing in…';
    };

    const completeAuthentication = async (request, completedLabel) => {
      status.textContent = `${completedLabel}. Verifying secure session…`;
      const session = await api.session({signal: request.signal});
      if (!isCurrent(request)) return;
      if (!session.authenticated) {
        const retentionError = new Error(
          `${completedLabel}, but the secure session was not retained. Open OMP Deck over HTTPS.`
        );
        retentionError.name = 'SessionRetentionError';
        throw retentionError;
      }
      if (api.mode() === 'coordinator') {
        status.textContent = `${completedLabel}. Reloading the authenticated dashboard…`;
        checkController = null;
        location.reload();
        return;
      }
      checkController = null;
      await showApplication();
    };

    const signInWithTailscale = async request => {
      showTailscaleChecking();
      try {
        await api.tailscaleLogin({signal: request.signal});
        if (!isCurrent(request)) return;
        await completeAuthentication(request, 'Signed in with Tailscale');
      } catch (caught) {
        if (!isCurrent(request)) return;
        checkController = null;
        if (caught.name === 'TailscaleAccessDeniedError') {
          showTailscale({message: caught.message}, request);
        } else if (caught.name === 'SessionRetentionError') {
          showTailscale({retentionFailed: true, message: caught.message}, request);
        } else {
          showRetry(caught.message, request);
        }
      }
    };

    const check = async () => {
      const request = begin();
      showChecking();
      try {
        const session = await api.session({signal: request.signal});
        if (!isCurrent(request)) return;
        if (session.authenticated) {
          checkController = null;
          await showApplication();
        } else {
          resetApplication();
          if (api.authMode() === 'tailscale') {
            await signInWithTailscale(request);
          } else {
            checkController = null;
            showPairing({clearToken: true}, request);
          }
        }
      } catch (caught) {
        if (!isCurrent(request)) return;
        checkController = null;
        showRetry(caught.message, request);
      }
    };

    retry.addEventListener('click', () => { void check(); });
    tailscaleSignIn.addEventListener('click', () => {
      const request = begin();
      void signInWithTailscale(request);
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (api.authMode() !== 'pairing') return;
      const request = begin();
      error.hidden = true;
      status.textContent = 'Pairing…';
      submit.disabled = true;
      try {
        await api.pair(token.value.trim(), {signal: request.signal});
        if (!isCurrent(request)) return;
        await completeAuthentication(request, 'Paired');
      } catch (caught) {
        if (!isCurrent(request)) return;
        checkController = null;
        status.textContent = '';
        error.textContent = caught.message;
        error.hidden = false;
        if (focusUnchanged(request)) token.select();
      } finally {
        if (isCurrent(request)) submit.disabled = false;
      }
    });

    const retryIfDisconnected = () => {
      if (!retry.hidden || (checkController && fields.hidden && tailscaleSignIn.hidden)) {
        void check();
      }
    };
    window.addEventListener('online', retryIfDisconnected);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') retryIfDisconnected();
    });
    window.addEventListener('pageshow', event => {
      if (event.persisted) retryIfDisconnected();
    });

    window.addEventListener('omp-deck-auth-required', () => {
      resetApplication();
      showChecking();
      void check();
    });
    document.addEventListener('DOMContentLoaded', () => { void check(); }, {once: true});

    return Object.freeze({
      check,
      requireAuthentication(options = {}) {
        const request = begin();
        checkController = null;
        resetApplication();
        if (api.authMode() === 'tailscale') showTailscale({signedOut: true}, request);
        else showPairing(options, request);
      },
    });
  }

  window.createAuthRecovery = createAuthRecovery;
})();
