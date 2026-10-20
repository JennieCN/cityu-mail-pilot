/* Apply the cached appearance before the first paint.
 *
 * This runs as a separate file rather than an inline <script> because the app
 * serves `script-src 'self'`; an inline block is blocked by that policy, which
 * silently reduced this to a no-op (the theme then only arrived once app.js had
 * loaded, flashing the default palette on every reload).
 *
 * localStorage is only a cache for the logged-out screen and for hiding that
 * flash: the account profile on the server is the source of truth, and app.js
 * reconciles the two as soon as /api/me answers.
 */
(function () {
  var KNOWN_THEMES = ['paper', 'dusk', 'harbour', 'night', 'classic'];
  var KNOWN_BACKGROUNDS = ['', 'none', 'paper', 'dusk', 'harbour', 'night'];
  try {
    var theme = localStorage.getItem('pilot.appearance.theme');
    if (KNOWN_THEMES.indexOf(theme) !== -1) {
      document.documentElement.dataset.theme = theme;
    }
    var background = localStorage.getItem('pilot.appearance.background');
    if (KNOWN_BACKGROUNDS.indexOf(background) !== -1 && background) {
      document.documentElement.style.setProperty(
        '--bg-image', background === 'none' ? 'none' : 'url("/bg-' + background + '.png")');
    }
  } catch (error) {
    /* Private mode or a blocked storage API: keep the default look. */
  }
})();
