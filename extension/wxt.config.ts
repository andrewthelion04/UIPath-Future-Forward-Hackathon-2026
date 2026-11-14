import { defineConfig } from 'wxt';

// Config-ul rulează în Node la build; declarăm doar ce folosim, fără dependența @types/node.
declare const process: { env: Record<string, string | undefined> };

const DEFAULT_API_HOSTS = ['http://127.0.0.1:8000/*', 'http://localhost:8000/*'];

// Permisiunile de rețea urmează adresa API-ului folosită la build: un bundle care apelează
// alt host decât cel din manifest ar fi blocat de Chrome.
function apiHosts(): string[] {
  const configured = (process.env.WXT_API_URL ?? '').trim();
  if (!configured) return DEFAULT_API_HOSTS;
  try {
    return [`${new URL(configured).origin}/*`];
  } catch {
    throw new Error(`WXT_API_URL is not a valid URL: ${configured}`);
  }
}

// Extensia consumă același API REST ca simulatorul Streamlit.
// Adresa API-ului se poate schimba la build cu variabila WXT_API_URL (vezi lib/api.ts).
export default defineConfig({
  modules: ['@wxt-dev/module-react'],
  manifest: () => ({
    name: 'StayPilot AI',
    description: 'Host copilot: properties, notifications and approvals in the side panel.',
    permissions: ['sidePanel'],
    host_permissions: apiHosts(),
    action: { default_title: 'Open StayPilot AI' },
  }),
});
