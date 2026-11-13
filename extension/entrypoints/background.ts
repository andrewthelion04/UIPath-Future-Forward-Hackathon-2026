export default defineBackground(() => {
  // Clic pe iconița extensiei deschide panoul lateral.
  browser.sidePanel
    .setPanelBehavior({ openPanelOnActionClick: true })
    .catch((error: unknown) => console.error('StayPilot: cannot configure the side panel', error));
});
