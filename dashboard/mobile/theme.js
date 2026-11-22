/* Apply an explicit reader theme before the stylesheet loads. */
(() => {
  'use strict';
  const key = 'orrery.mobile.theme.v1';
  const root = document.documentElement;
  if (location.pathname === '/reader-pane') root.dataset.readerPane = '';
  const light = window.AgentStackColorThemeCore.deriveWarmPaperLightTheme().cssVariables;
  const themeColor = document.querySelector('meta[name="theme-color"]');
  const darkCanvas = themeColor.content;
  let theme = 'dark';
  try {
    if (localStorage.getItem(key) === 'light') theme = 'light';
  } catch (_) {
    // A browser may disable storage. The current page can still switch themes.
  }

  function apply() {
    for (const [property, value] of Object.entries(light)) {
      if (theme === 'light') root.style.setProperty(property, value);
      else root.style.removeProperty(property);
    }
    root.dataset.colorTheme = theme;
    document.querySelector('meta[name="color-scheme"]').content = theme;
    themeColor.content = theme === 'light' ? light['--bg'] : darkCanvas;
    const button = document.getElementById('mobile-theme');
    if (!button) return;
    document.querySelector('.mobile-brand img').src = theme === 'light' ? '/assets/orbit-light.svg' : '/assets/orbit.svg';
    const label = theme === 'dark' ? 'Use light theme' : 'Use dark theme';
    button.title = label;
    button.setAttribute('aria-label', label);
    button.querySelector('use').setAttribute('href', theme === 'dark' ? '#icon-sun' : '#icon-moon');
  }

  apply();
  window.addEventListener('storage', event => {
    if (event.key !== key) return;
    theme = event.newValue === 'light' ? 'light' : 'dark';
    apply();
  });
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    document.getElementById('mobile-theme').addEventListener('click', () => {
      theme = theme === 'dark' ? 'light' : 'dark';
      try {
        localStorage.setItem(key, theme);
      } catch (_) {
        // Keep the choice for this page.
      }
      apply();
    });
  }, {once: true});
})();
