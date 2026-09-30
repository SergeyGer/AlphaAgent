import type { ReactElement } from 'react';
import { Dashboard } from './components/Dashboard';
import { LoginScreen } from './components/LoginScreen';
import { AuthProvider, useAuth } from './hooks/useAuth';

function AuthGate(): ReactElement {
  const { isAuthenticated } = useAuth();
  return isAuthenticated ? <Dashboard /> : <LoginScreen />;
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
