import { useState, type FormEvent, type ReactElement } from 'react';
import { API_BASE, errorMessage } from '../api/client';
import { useAuth } from '../hooks/useAuth';
import { Spinner } from './Feedback';

/** Token login screen — `POST /api/auth/token/` with JSON credentials. */
export function LoginScreen(): ReactElement {
  const { login } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting) return;

    const trimmed = username.trim();
    if (trimmed === '' || password === '') {
      setError('Enter both your username and password.');
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      await login(trimmed, password);
    } catch (cause) {
      setError(
        errorMessage(cause) === 'Network error — the API is unreachable.'
          ? 'Cannot reach the AlphaAgent API. Is the backend running?'
          : errorMessage(cause),
      );
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-950 px-4 py-10">
      <div
        aria-hidden="true"
        className="pointer-events-none fixed inset-0 bg-[radial-gradient(ellipse_at_top,rgba(16,185,129,0.10),transparent_55%)]"
      />
      <div className="relative w-full max-w-sm">
        <div className="mb-6 flex items-center gap-3">
          <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-gradient-to-br from-emerald-400 to-sky-500 font-mono text-lg font-bold text-slate-950">
            α
          </span>
          <div>
            <h1 className="text-lg font-semibold tracking-tight text-slate-100">AlphaAgent</h1>
            <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-slate-500">
              Autonomous Investment Terminal
            </p>
          </div>
        </div>

        <form
          onSubmit={onSubmit}
          className="space-y-4 rounded-xl border border-slate-800 bg-slate-900/70 p-5 shadow-panel backdrop-blur"
        >
          <div className="space-y-1.5">
            <label htmlFor="username" className="block font-mono text-[10px] uppercase tracking-[0.16em] text-slate-400">
              Username
            </label>
            <input
              id="username"
              name="username"
              type="text"
              autoComplete="username"
              autoFocus
              spellCheck={false}
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              disabled={submitting}
              className="w-full rounded-md border border-slate-700 bg-slate-950/80 px-3 py-2 font-mono text-sm text-slate-100 placeholder:text-slate-600 focus:border-emerald-500/60 focus:outline-none focus:ring-1 focus:ring-emerald-500/40 disabled:opacity-60"
              placeholder="demo"
            />
          </div>

          <div className="space-y-1.5">
            <label htmlFor="password" className="block font-mono text-[10px] uppercase tracking-[0.16em] text-slate-400">
              Password
            </label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              disabled={submitting}
              className="w-full rounded-md border border-slate-700 bg-slate-950/80 px-3 py-2 font-mono text-sm text-slate-100 placeholder:text-slate-600 focus:border-emerald-500/60 focus:outline-none focus:ring-1 focus:ring-emerald-500/40 disabled:opacity-60"
              placeholder="••••••••"
            />
          </div>

          {error ? (
            <p
              role="alert"
              className="rounded-md border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs leading-relaxed text-rose-200"
            >
              {error}
            </p>
          ) : null}

          <button
            type="submit"
            disabled={submitting}
            className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-emerald-500 px-3 py-2 text-sm font-semibold text-slate-950 transition-colors hover:bg-emerald-400 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-300 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {submitting ? <Spinner className="border-slate-900/40 border-t-slate-900" /> : null}
            {submitting ? 'Authenticating…' : 'Sign in'}
          </button>

          <p className="border-t border-slate-800 pt-3 font-mono text-[10px] leading-relaxed text-slate-600">
            POST {API_BASE || ''}/api/auth/token/
          </p>
        </form>
      </div>
    </div>
  );
}
