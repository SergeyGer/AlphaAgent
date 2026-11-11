import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { ViewerApp } from './ViewerApp';
import { installOfflineFetchGuard } from './offline';
import '../src/index.css';

installOfflineFetchGuard();

const container = document.getElementById('root');

if (!container) {
  throw new Error('AlphaAgent offline viewer: #root container is missing from index.html');
}

createRoot(container).render(
  <StrictMode>
    <ViewerApp />
  </StrictMode>,
);
