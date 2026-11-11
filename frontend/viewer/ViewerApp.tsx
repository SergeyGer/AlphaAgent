import { useCallback, useEffect, useMemo, useState, type ChangeEvent, type ReactElement } from 'react';
import { cn } from '../src/lib/cn';
import { Badge } from '../src/components/Badge';
import {
  BundleValidationError,
  SOURCE_LABEL_DESCRIPTION,
  SOURCE_LABEL_TEXT,
  VIEWER_SOURCE_LABELS,
  parseViewerBundleText,
  type ImportedBundle,
  type ViewerSourceLabel,
} from './bundle';
import { ViewerRunView } from './ViewerRunView';

/**
 * REP-02 — the no-database, no-login, no-network viewer.
 *
 * The default bundle is loaded with a same-origin `fetch` when the page is
 * served. A build-time copy is embedded as application/json so opening the
 * built file from disk also works; the file picker covers any other bundle
 * (including a real `GET /api/decision-runs/<id>/export/` capture).
 *
 * Nothing here talks to the authenticated API. There are no approve, execute,
 * run-agent or live-refresh controls, and the fetch guard only permits the
 * relative fixture file.
 */

type LoadOrigin = 'fetched' | 'embedded' | 'file';

interface LoadedBundle {
  bundle: ImportedBundle;
  origin: LoadOrigin;
  fileName: string | null;
}

function errorText(cause: unknown): { message: string; code: string | null } {
  if (cause instanceof BundleValidationError) {
    return { message: cause.message, code: cause.code };
  }
  if (cause instanceof Error) return { message: cause.message, code: null };
  return { message: 'The bundle could not be imported.', code: null };
}

function embeddedBundleText(): string | null {
  const element = document.getElementById('viewer-default-bundle');
  const text = element?.textContent ?? '';
  return text.trim() === '' ? null : text;
}

async function fetchDefaultBundle(): Promise<string | null> {
  if (typeof window === 'undefined' || typeof window.fetch !== 'function') return null;
  // A file:// document has no same-origin HTTP fetch; the embedded copy is the
  // documented fallback there. Trying anyway would only produce a console error.
  if (window.location.protocol === 'file:') return null;
  try {
    const response = await window.fetch('./decision-runs.v1.bundle.json', {
      cache: 'no-store',
      credentials: 'omit',
    });
    if (!response.ok) return null;
    return await response.text();
  } catch {
    return null;
  }
}

const CONTROL_CLASS =
  'inline-flex min-h-[36px] items-center justify-center rounded-md border border-slate-700 bg-slate-950/70 px-2.5 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wider text-slate-200 transition-colors hover:bg-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950';

function SourceLegend(): ReactElement {
  return (
    <details className="rounded-lg border border-slate-800/70 bg-slate-950/40 px-3 py-2">
      <summary className="cursor-pointer font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
        Source label meanings
      </summary>
      <dl className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
        {VIEWER_SOURCE_LABELS.map((label) => (
          <div key={label} className="min-w-0">
            <dt className="font-mono text-[10px] font-semibold uppercase tracking-wider text-slate-300">
              {SOURCE_LABEL_TEXT[label as ViewerSourceLabel]}
            </dt>
            <dd className="mt-0.5 text-xs leading-relaxed text-slate-400">
              {SOURCE_LABEL_DESCRIPTION[label as ViewerSourceLabel]}
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

export function ViewerApp(): ReactElement {
  const [loaded, setLoaded] = useState<LoadedBundle | null>(null);
  const [loading, setLoading] = useState(true);
  const [importError, setImportError] = useState<{ message: string; code: string | null } | null>(
    null,
  );
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const fetched = await fetchDefaultBundle();
      if (cancelled) return;

      // A served bundle that exists but fails validation is a corrupted file,
      // not a reason to silently substitute the embedded copy. Fetch failure or
      // absence (including file://) is the only path that uses the embedded
      // default.
      if (fetched !== null) {
        try {
          const bundle = parseViewerBundleText(fetched);
          setLoaded({
            bundle,
            origin: 'fetched',
            fileName: 'decision-runs.v1.bundle.json',
          });
          setSelectedRunId(bundle.runs[0]?.run_id ?? null);
          setImportError(null);
        } catch (cause) {
          setImportError(errorText(cause));
        }
        setLoading(false);
        return;
      }

      const embedded = embeddedBundleText();
      if (embedded !== null) {
        try {
          const bundle = parseViewerBundleText(embedded);
          setLoaded({
            bundle,
            origin: 'embedded',
            fileName: 'decision-runs.v1.bundle.json (embedded)',
          });
          setSelectedRunId(bundle.runs[0]?.run_id ?? null);
          setImportError(null);
        } catch (cause) {
          setImportError(errorText(cause));
        }
      }
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const importText = useCallback((value: string, origin: LoadOrigin, fileName: string | null) => {
    try {
      const bundle = parseViewerBundleText(value);
      setLoaded({ bundle, origin, fileName });
      setSelectedRunId(bundle.runs[0]?.run_id ?? null);
      setImportError(null);
    } catch (cause) {
      setImportError(errorText(cause));
    }
  }, []);

  const handleFile = useCallback(
    async (event: ChangeEvent<HTMLInputElement>) => {
      const file = event.target.files?.[0];
      if (!file) return;
      try {
        const text = await file.text();
        importText(text, 'file', file.name);
      } catch (cause) {
        setImportError(errorText(cause));
      } finally {
        // Allow selecting the same file again after a rejection.
        event.target.value = '';
      }
    },
    [importText],
  );

  const selectedRun = useMemo(() => {
    if (!loaded) return null;
    return (
      loaded.bundle.runs.find((run) => run.run_id === selectedRunId) ??
      loaded.bundle.runs[0] ??
      null
    );
  }, [loaded, selectedRunId]);

  return (
    <div className="min-h-screen bg-slate-950">
      <header className="border-b border-slate-800/80 bg-slate-900/40 px-3 py-3 sm:px-4">
        <div className="mx-auto flex max-w-[1500px] flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
              AlphaAgent · REP-02
            </p>
            <h1 className="mt-0.5 text-lg font-semibold tracking-tight text-slate-100">
              Offline decision viewer
            </h1>
            <p className="mt-0.5 max-w-3xl text-sm leading-relaxed text-slate-400">
              A static, read-only page that plays a stored fixture bundle. No Django, no
              PostgreSQL, no Redis, no login and no model key.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge tone="emerald">Read-only</Badge>
            <Badge tone="sky">No database</Badge>
            <Badge tone="slate">No login</Badge>
            <Badge tone="violet">No model key</Badge>
            <Badge tone="rose">No execution</Badge>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1500px] space-y-3 px-3 py-4 sm:px-4">
        <section
          aria-label="Bundle import"
          className="rounded-xl border border-slate-800/80 bg-slate-900/50 p-3 shadow-panel sm:p-4"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="min-w-0">
              <h2 className="font-mono text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-400">
                Loaded bundle
              </h2>
              <p className="mt-0.5 text-sm text-slate-300">
                {loaded
                  ? `${loaded.bundle.runs.length} run${loaded.bundle.runs.length === 1 ? '' : 's'} · ${SOURCE_LABEL_TEXT[loaded.bundle.sourceLabel]} · schema ${loaded.bundle.schemaVersion} · ${loaded.origin === 'file' ? 'file' : loaded.origin}`
                  : loading
                    ? 'Loading the default fixture bundle…'
                    : 'No bundle loaded.'}
              </p>
              {loaded ? (
                <p className="mt-0.5 text-xs leading-relaxed text-emerald-300/90">
                  Schema and SHA-256 checksum verified before import.
                </p>
              ) : null}
              {loaded?.fileName ? (
                <p className="mt-0.5 break-all font-mono text-[10px] uppercase tracking-wider text-slate-500">
                  {loaded.fileName}
                </p>
              ) : null}
            </div>
            <label
              className={cn(
                CONTROL_CLASS,
                'cursor-pointer focus-within:ring-2 focus-within:ring-cyan-400/70 focus-within:ring-offset-2 focus-within:ring-offset-slate-950',
              )}
              aria-label="Load a decision-run bundle file"
            >
              <span>Load bundle file</span>
              <input
                type="file"
                accept="application/json,.json"
                onChange={(event) => {
                  void handleFile(event);
                }}
                className="sr-only"
                data-testid="viewer-file-input"
              />
            </label>
          </div>

          <div className="mt-2">
            <SourceLegend />
          </div>

          {importError ? (
            <div
              role="alert"
              data-state="bundle-error"
              data-error-code={importError.code ?? undefined}
              className="mt-2 rounded-lg border border-rose-500/40 bg-rose-500/[0.08] px-3 py-2"
            >
              <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-rose-300">
                Bundle rejected
              </p>
              <p className="mt-1 break-words text-sm leading-relaxed text-slate-200">
                {importError.message}
              </p>
            </div>
          ) : null}

          {!loading && !loaded && !importError ? (
            <p className="mt-2 rounded-lg border border-dashed border-slate-800 bg-slate-950/40 px-3 py-2 text-sm text-slate-400">
              The default bundle could not be loaded. Choose a decision-run bundle file above; the
              file is parsed locally and nothing is uploaded.
            </p>
          ) : null}
        </section>

        {loaded && loaded.bundle.runs.length > 1 ? (
          <nav aria-label="Stored runs" className="min-w-0">
            <p className="px-1 font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
              Choose a stored run
            </p>
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {loaded.bundle.runs.map((run) => {
                const active = run.run_id === selectedRun?.run_id;
                return (
                  <button
                    key={run.run_id}
                    type="button"
                    onClick={() => setSelectedRunId(run.run_id)}
                    aria-pressed={active}
                    className={cn(
                      'min-h-[36px] max-w-full rounded-md border px-2.5 py-1.5 text-left text-sm transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950',
                      active
                        ? 'border-cyan-500/50 bg-cyan-500/10 text-cyan-100'
                        : 'border-slate-700 bg-slate-950/50 text-slate-300 hover:bg-slate-800',
                    )}
                  >
                    <span className="mr-2 font-mono text-xs font-semibold">
                      {run.detail.run.ticker}
                    </span>
                    <span className="break-all font-mono text-[10px] uppercase tracking-wider text-slate-400">
                      {SOURCE_LABEL_TEXT[run.source_label]} · {run.detail.run.status}
                    </span>
                  </button>
                );
              })}
            </div>
          </nav>
        ) : null}

        {selectedRun ? (
          <ViewerRunView key={selectedRun.run_id} run={selectedRun} />
        ) : null}

        <footer className="rounded-lg border border-slate-800/70 bg-slate-950/40 px-3 py-2 text-xs leading-relaxed text-slate-400">
          <p>
            This page is a static replay. It contains no execution, approval or live-analysis
            control, and it makes no request to a model provider, the AlphaAgent API or an execution
            endpoint. The only permitted network read is the relative fixture bundle; its schema
            and SHA-256 checksum are verified before any content is shown.
          </p>
          {loaded?.bundle.honesty ? (
            <p className="mt-1 text-slate-500">
              Bundle honesty notes are included with the fixture data; synthetic content is labelled
              synthetic on every run.
            </p>
          ) : null}
        </footer>
      </main>
    </div>
  );
}
