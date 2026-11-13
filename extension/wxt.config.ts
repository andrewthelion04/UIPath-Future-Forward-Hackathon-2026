import { defineConfig } from 'wxt';

// Extensia consumă același API REST ca simulatorul Streamlit.
// Adresa API-ului se poate schimba la build cu variabila WXT_API_URL (vezi lib/api.ts).
export default defineConfig({
  modules: ['@wxt-dev/module-react'],
  manifest: {
    name: 'StayPilot AI',
    description: 'Host copilot: properties, notifications and approvals in the side panel.',
    permissions: ['sidePanel'],
    host_permissions: ['http://127.0.0.1:8000/*', 'http://localhost:8000/*'],
    action: { default_title: 'Open StayPilot AI' },
  },
});
