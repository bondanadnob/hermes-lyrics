(function () {
  "use strict";

  var sdk = window.__HERMES_PLUGIN_SDK__;
  var registry = window.__HERMES_PLUGINS__;
  if (!sdk || !sdk.React || !registry) return;

  function DesktopOnlyNotice() {
    return sdk.React.createElement(
      "div",
      { className: "max-w-lg p-6 text-sm text-muted-foreground" },
      "Apple Music Lyrics is available in Hermes Desktop on the local Mac."
    );
  }

  registry.register("apple-music-lyrics", DesktopOnlyNotice);
})();
