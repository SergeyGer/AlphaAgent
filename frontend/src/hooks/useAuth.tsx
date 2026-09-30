import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactElement,
  type ReactNode,
} from 'react';
import { ApiError, getStoredUsername, getToken, onUnauthorized, setToken as persistToken } from '../api/client';
import { api } from '../api/client';

export interface AuthContextValue {
  token: string | null;
  username: string | null;
  isAuthenticated: boolean;
  /** Resolves on success; throws `ApiError` with a user-facing message. */
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }): ReactElement {
  const [token, setTokenState] = useState<string | null>(() => getToken());
  const [username, setUsername] = useState<string | null>(() => getStoredUsername());

  const logout = useCallback(() => {
    persistToken(null);
    setTokenState(null);
    setUsername(null);
  }, []);

  const login = useCallback(async (nextUsername: string, password: string) => {
    const response = await api.login(nextUsername, password);
    const nextToken = response?.token;
    if (typeof nextToken !== 'string' || nextToken === '') {
      throw new ApiError(500, 'The server did not return an authentication token.');
    }
    persistToken(nextToken, nextUsername);
    setTokenState(nextToken);
    setUsername(nextUsername);
  }, []);

  // Any 401 from any REST call drops the session and returns to the login screen.
  useEffect(
    () =>
      onUnauthorized(() => {
        setTokenState(null);
        setUsername(null);
      }),
    [],
  );

  const value = useMemo<AuthContextValue>(
    () => ({ token, username, isAuthenticated: token !== null, login, logout }),
    [token, username, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used inside <AuthProvider>.');
  return context;
}
