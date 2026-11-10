import {
  useEffect,
  useId,
  useRef,
  useState,
  type MouseEvent,
  type ReactElement,
  type ReactNode,
} from 'react';
import { cn } from '../lib/cn';
import {
  evidenceQualityFlags,
  formatRunTimestamp,
  isExternalHttpUrl,
  presentText,
} from '../lib/decisionRuns';
import type { EvidenceItem, EvidenceObservation } from '../types';

/**
 * UX-02 — the stored source behind a claim.
 *
 * Everything shown here comes from the already-fetched detail payload: opening
 * the drawer performs no request, so reading evidence during replay cannot
 * disturb the run's event cursor.
 *
 * The `published_at: null` case is treated as a first-class value. A source
 * that did not publish a timestamp says so in words; it never renders a zero,
 * an epoch date, or a dash that could be mistaken for a date.
 */

export interface EvidenceDrawerProps {
  open: boolean;
  evidence: EvidenceItem[];
  /** Item opened from a specific claim/rebuttal. Falls back to the first item. */
  initialEvidenceId?: string | null;
  onClose: () => void;
  className?: string;
}

const CONTROL_CLASS =
  'rounded-md border border-slate-700 bg-slate-900/80 px-2.5 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wider text-slate-200 transition-colors hover:bg-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950';

function Field({
  label,
  children,
  testId,
}: {
  label: string;
  children: ReactNode;
  testId?: string;
}): ReactElement {
  return (
    <div className="min-w-0 border-b border-slate-800/60 py-2 last:border-0">
      <dt className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
        {label}
      </dt>
      <dd data-testid={testId} className="mt-0.5 break-words text-sm leading-relaxed text-slate-200">
        {children}
      </dd>
    </div>
  );
}

function ObservationBlock({ observation }: { observation: EvidenceObservation | null }): ReactElement {
  return (
    <section aria-labelledby="evidence-observation" className="min-w-0">
      <h3
        id="evidence-observation"
        className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400"
      >
        Typed observation
      </h3>
      {observation ? (
        <div className="mt-1 flex flex-wrap items-baseline gap-2">
          <p className="break-all font-mono text-base font-semibold tabular-nums text-cyan-200">
            {observation.value}
          </p>
          {presentText(observation.unit) ? (
            <span className="text-sm text-slate-300">{observation.unit}</span>
          ) : (
            <span className="text-sm text-slate-400">unit not stated</span>
          )}
          {presentText(observation.currency) ? (
            <span className="rounded border border-slate-700 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-slate-300">
              {observation.currency}
            </span>
          ) : null}
        </div>
      ) : (
        <p className="mt-1 text-sm text-slate-400">No typed observation stored for this source.</p>
      )}
    </section>
  );
}

function EvidenceDetails({ item }: { item: EvidenceItem }): ReactElement {
  const flags = evidenceQualityFlags(item);
  const excerpt = presentText(item.excerpt);
  const publisher = presentText(item.publisher);
  const sourceType = presentText(item.source_type);
  const sourceUri = presentText(item.source_uri);
  const linked = isExternalHttpUrl(sourceUri);
  const publishedKnown = presentText(item.published_at) !== null;

  return (
    <article aria-label={`Evidence ${item.id}`} className="min-w-0 space-y-4">
      <header className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="break-all font-mono text-sm font-semibold text-slate-100">{item.id}</h3>
          <span className="rounded border border-slate-700 bg-slate-900 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-slate-300">
            {sourceType ?? 'Source type unknown'}
          </span>
        </div>
        <p className="mt-1 text-sm text-slate-400">
          {publisher ?? 'Publisher unknown'}
        </p>
      </header>

      <ObservationBlock observation={item.observation} />

      <section aria-labelledby="evidence-excerpt" className="min-w-0">
        <h3
          id="evidence-excerpt"
          className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400"
        >
          Stored excerpt
        </h3>
        {excerpt ? (
          <blockquote className="mt-1 whitespace-pre-wrap break-words border-l-2 border-slate-700 pl-3 text-sm leading-relaxed text-slate-200">
            {excerpt}
          </blockquote>
        ) : (
          <p className="mt-1 text-sm text-slate-400">No excerpt stored for this source.</p>
        )}
      </section>

      <dl className="min-w-0">
        <Field label="Publisher">{publisher ?? 'Unknown — the source supplied no publisher.'}</Field>
        <Field label="Published at" testId="evidence-published-value">
          {publishedKnown ? (
            <time dateTime={item.published_at ?? undefined}>{formatRunTimestamp(item.published_at)}</time>
          ) : (
            <span className="text-amber-300">
              Unknown — the source did not publish a timestamp.
            </span>
          )}
        </Field>
        <Field label="Retrieved at">
          <time dateTime={item.retrieved_at || undefined}>{formatRunTimestamp(item.retrieved_at)}</time>
        </Field>
        <Field label="Source link">
          {linked && sourceUri ? (
            <a
              href={sourceUri}
              target="_blank"
              rel="noopener noreferrer"
              className="break-all text-cyan-300 underline decoration-cyan-500/40 underline-offset-2 hover:text-cyan-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950"
            >
              {sourceUri}
            </a>
          ) : sourceUri ? (
            <span className="break-all text-slate-300">
              {sourceUri} <span className="text-amber-300">(not an http(s) link; not clickable)</span>
            </span>
          ) : (
            <span className="text-slate-400">No source link stored.</span>
          )}
        </Field>
        <Field label="Content hash">
          {presentText(item.content_hash) ? (
            <code className="break-all font-mono text-xs text-slate-300">{item.content_hash}</code>
          ) : (
            <span className="text-slate-400">Not recorded.</span>
          )}
        </Field>
      </dl>

      <section aria-label="Data quality flags" className="min-w-0">
        <h3 className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
          Data quality
        </h3>
        {flags.length === 0 ? (
          <p className="mt-1 text-sm text-slate-400">No data-quality flags on this item.</p>
        ) : (
          <ul className="mt-1 space-y-1">
            {flags.map((flag) => (
              <li key={flag} className="flex gap-2 text-sm leading-relaxed text-amber-200">
                <span aria-hidden="true" className="font-mono text-xs text-amber-400">
                  !
                </span>
                <span>{flag}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </article>
  );
}

export function EvidenceDrawer({
  open,
  evidence,
  initialEvidenceId = null,
  onClose,
  className,
}: EvidenceDrawerProps): ReactElement | null {
  const titleId = useId();
  const descriptionId = useId();
  const panelRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  const items = Array.isArray(evidence) ? evidence.filter(Boolean) : [];
  const [selectedId, setSelectedId] = useState<string | null>(
    initialEvidenceId ?? items[0]?.id ?? null,
  );

  // Reset the selection each time the drawer is (re)opened, preferring the item
  // that the claim/rebuttal linked to.
  useEffect(() => {
    if (!open) return;
    const preferred =
      initialEvidenceId !== null && items.some((item) => item.id === initialEvidenceId)
        ? initialEvidenceId
        : (items[0]?.id ?? null);
    setSelectedId(preferred);
    // `items` intentionally tracked through the evidence payload identity.
  }, [open, initialEvidenceId, evidence]);

  // Focus management: move focus in, trap Tab inside the drawer, restore the
  // opener on close. Escape is handled at the document level so it works from
  // anywhere inside the panel.
  useEffect(() => {
    if (!open) return undefined;

    restoreFocusRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const bodyOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    const focusTimer = window.setTimeout(() => closeRef.current?.focus(), 0);

    const handleKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab') return;

      const panel = panelRef.current;
      if (!panel) return;
      const focusable = Array.from(
        panel.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;

      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => {
      window.clearTimeout(focusTimer);
      document.removeEventListener('keydown', handleKeyDown);
      document.body.style.overflow = bodyOverflow;
      restoreFocusRef.current?.focus?.();
    };
  }, [open]);

  if (!open) return null;

  const selected = items.find((item) => item.id === selectedId) ?? items[0] ?? null;

  const handleBackdropMouseDown = (event: MouseEvent<HTMLDivElement>): void => {
    if (event.target === event.currentTarget) onCloseRef.current();
  };

  return (
    <div
      className={cn(
        'fixed inset-0 z-50 flex justify-end bg-slate-950/80 backdrop-blur-sm sm:p-4',
        className,
      )}
      onMouseDown={handleBackdropMouseDown}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        className="flex h-full min-h-0 w-full max-w-2xl min-w-0 flex-col overflow-hidden border border-slate-700/80 bg-slate-950 shadow-panel sm:rounded-xl"
      >
        <header className="flex items-start justify-between gap-3 border-b border-slate-800/80 px-4 py-3">
          <div className="min-w-0">
            <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-cyan-300">
              UX-02 · Evidence
            </p>
            <h2 id={titleId} className="mt-0.5 text-base font-semibold text-slate-100">
              Stored evidence
            </h2>
            <p id={descriptionId} className="mt-0.5 text-sm text-slate-400">
              Read-only record. Opening this drawer performs no fetch.
            </p>
          </div>
          <button
            ref={closeRef}
            type="button"
            onClick={() => onCloseRef.current()}
            aria-label="Close evidence drawer"
            className={CONTROL_CLASS}
          >
            Close
          </button>
        </header>

        <div className="flex min-h-0 flex-1 flex-col sm:flex-row">
          <nav
            aria-label="Stored evidence items"
            className="min-w-0 shrink-0 border-b border-slate-800/80 sm:w-60 sm:border-b-0 sm:border-r"
          >
            {items.length === 0 ? (
              <p className="p-4 text-sm text-slate-400">No stored evidence for this run.</p>
            ) : (
              <ul className="max-h-40 space-y-1 overflow-y-auto p-2 sm:max-h-none sm:h-full">
                {items.map((item, index) => {
                  const isSelected = item.id === selected?.id;
                  return (
                    <li key={item.id || `evidence-${index}`} className="min-w-0">
                      <button
                        type="button"
                        onClick={() => setSelectedId(item.id)}
                        aria-pressed={isSelected}
                        aria-label={`Evidence ${index + 1}: ${
                          presentText(item.publisher) ?? presentText(item.source_type) ?? item.id
                        }`}
                        className={cn(
                          'w-full min-w-0 rounded-md border px-2 py-1.5 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70',
                          isSelected
                            ? 'border-cyan-500/50 bg-cyan-500/10'
                            : 'border-transparent hover:border-slate-700 hover:bg-slate-900/70',
                        )}
                      >
                        <span className="block truncate text-sm text-slate-200">
                          {presentText(item.publisher) ?? 'Publisher unknown'}
                        </span>
                        <span className="block truncate font-mono text-[10px] uppercase tracking-wider text-slate-400">
                          {presentText(item.source_type) ?? 'source type unknown'} · {item.id}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </nav>

          <div className="min-h-0 min-w-0 flex-1 overflow-y-auto p-4">
            {selected ? (
              <EvidenceDetails item={selected} />
            ) : (
              <div className="rounded-lg border border-dashed border-slate-800 bg-slate-900/30 px-4 py-6">
                <p className="font-mono text-xs uppercase tracking-wider text-slate-400">
                  No evidence stored
                </p>
                <p className="mt-1 text-sm leading-relaxed text-slate-400">
                  The claim may reference evidence that was not persisted with this run. Nothing is
                  reconstructed in its place.
                </p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
