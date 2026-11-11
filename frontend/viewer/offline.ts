/**
 * The viewer's network guard.
 *
 * A static page can still call `fetch`; the acceptance requirement is that it
 * makes no request to a provider or execution endpoint. Rather than trusting
 * the absence of such code, the viewer installs a wrapper that only permits the
 * one relative fixture-bundle request it actually needs. The Content-Security-
 * Policy in `index.html` enforces the same rule at the browser level.
 */

const BUNDLE_PATH_PATTERN = /(?:^|\/)decision-runs\.[\w.-]*bundle\.json$/i;

export function isAllowedViewerRequest(input: RequestInfo | URL, baseHref: string): boolean {
  let url: URL;
  try {
    const raw =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    url = new URL(raw, baseHref);
  } catch {
    return false;
  }

  const base = new URL(baseHref);
  if (url.origin !== base.origin) return false;
  return BUNDLE_PATH_PATTERN.test(url.pathname);
}

export function installOfflineFetchGuard(): void {
  if (typeof window === 'undefined' || typeof window.fetch !== 'function') return;
  if ((window as { __alphaAgentOfflineGuard?: boolean }).__alphaAgentOfflineGuard) return;
  (window as { __alphaAgentOfflineGuard?: boolean }).__alphaAgentOfflineGuard = true;

  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    if (!isAllowedViewerRequest(input, window.location.href)) {
      return Promise.reject(
        new Error(
          'Offline viewer: network access is disabled. Only the relative fixture bundle may be loaded.',
        ),
      );
    }
    return nativeFetch(input, init);
  };
}
