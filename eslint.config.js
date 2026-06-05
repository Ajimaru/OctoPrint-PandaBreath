// ESLint flat config (ESLint v9+). The pre-commit eslint hook only lints
// the plugin's own static JS; vendored/library files are excluded via the
// hook's `exclude` pattern in .pre-commit-config.yaml.
const globals = require("globals");

module.exports = [
  {
    files: ["**/*.js"],
    languageOptions: {
      ecmaVersion: 2021,
      sourceType: "script",
      globals: {
        ...globals.es2021,
        ...globals.browser,
        ...globals.node,
        // OctoPrint frontend globals provided at runtime by the web app.
        $: "readonly",
        ko: "readonly",
        OctoPrint: "readonly",
        PNotify: "readonly",
        gettext: "readonly",
        OCTOPRINT_VIEWMODELS: "readonly",
      },
    },
    rules: {},
  },
];
