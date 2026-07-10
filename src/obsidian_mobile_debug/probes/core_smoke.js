// core_smoke.js - a plugin-agnostic health check for the Obsidian page.
//
// Returns { ok, failures, ... } describing the live runtime: vault, platform,
// Obsidian API version, and installed/enabled plugin counts. It makes no
// assumptions about any specific plugin, so it is a safe first probe to confirm
// the transport works and `app` is reachable. `omd ... eval` maps a truthy
// `ok:false` to a non-zero exit code, which makes this usable as a CI gate.
(async () => {
	const failures = [];

	if (typeof app === "undefined" || !app) {
		return { ok: false, failures: ["Obsidian `app` global is not available in this page."] };
	}

	const vaultName = app.vault?.getName?.() ?? null;
	if (!vaultName) failures.push("app.vault.getName() returned no vault name.");

	if (!app.workspace?.layoutReady) failures.push("app.workspace.layoutReady is false.");

	const installed = Object.keys(app.plugins?.plugins ?? {});
	const enabled = Array.from(app.plugins?.enabledPlugins ?? []);
	const apiVersionValue = (typeof apiVersion !== "undefined" ? apiVersion : (window.apiVersion ?? null));

	return {
		ok: failures.length === 0,
		failures,
		vault: vaultName,
		platform: app.isMobile ? "mobile" : "desktop",
		obsidianApiVersion: apiVersionValue,
		installedPluginCount: installed.length,
		enabledPluginCount: enabled.length,
		enabledPlugins: enabled.slice(0, 50),
	};
})()
