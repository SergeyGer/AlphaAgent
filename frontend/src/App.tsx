import type { ReactElement } from 'react';
import { Dashboard } from './components/Dashboard';
import { DecisionRunsPage } from './components/DecisionRunsPage';
import { LoginScreen } from './components/LoginScreen';
import { PortfolioPlansPage } from './components/PortfolioPlansPage';
import { AuthProvider, useAuth } from './hooks/useAuth';
import { useHashRoute } from './hooks/useHashRoute';

function AuthGate(): ReactElement {
  const { isAuthenticated } = useAuth();
  const route = useHashRoute();
  if (!isAuthenticated) return <LoginScreen />;
  // Phase 1 addendum: a hash-addressed explain screen sits alongside the
  // existing dashboard rather than restructuring it.
  if (route.view === 'runs') return <DecisionRunsPage runId={route.runId} />;
  // FIX-08: the advisory plan review/approval workflow is a sibling route, not
  // a dashboard panel, so the existing layout is untouched.
  if (route.view === 'plans') return <PortfolioPlansPage planId={route.planId} />;
  return <Dashboard />;
}

/**
 * AlphaAgent terminal.
 *
 * `AuthProvider` owns the token (localStorage + `Authorization: Token …`);
 * a 401 from any REST call clears it and drops the user back to the login
 * screen automatically.
 */
export function App(): ReactElement {
  return (
    <AuthProvider>
      <AuthGate />
    </AuthProvider>
  );
}
