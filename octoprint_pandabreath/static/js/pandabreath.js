/*
 * View model for OctoPrint-PandaBreath
 *
 * License: MIT
 */
$(function () {
    function PandabreathViewModel(parameters) {
        var self = this;

        self.loginState = parameters[0];
        self.settings = parameters[1];
        // OctoPrint's own printerStateViewModel — the live print-job state
        // of the printer this Panda Breath is paired with. NB: kept distinct
        // from self.printerState below, which is the *Panda Breath device's*
        // internal printer_state code (binding/reachability). Same word, two very
        // different sources; do not merge them.
        self.octoprintState = parameters[2];

        // True while the OctoPrint printer this Panda is paired with is
        // running a job (or starting/pausing/cancelling/paused). The
        // chamber's dry-mode is for drying filament, not printing, so we
        // disable dry-mode and Start Drying for the whole active window.
        // The printerStateViewModel observables are reactive, so the
        // buttons re-enable on their own once the print finishes. The
        // backend enforces the same rule (409) in case of a direct API
        // call — this is purely the UX gate.
        self.octoprintBusy = ko.pureComputed(function () {
            var ps = self.octoprintState;
            if (!ps) return false;
            return !!(
                ko.unwrap(ps.isPrinting) ||
                ko.unwrap(ps.isStarting) ||
                ko.unwrap(ps.isPausing) ||
                ko.unwrap(ps.isPaused) ||
                ko.unwrap(ps.isResuming) ||
                ko.unwrap(ps.isCancelling)
            );
        });

        self.chamberTemp = ko.observable(null);
        self.targetTemp = ko.observable(0);
        self.heaterOn = ko.observable(false);
        self.mode = ko.observable("auto");
        self.locked = ko.observable(false);
        self.lockReason = ko.observable("");
        self.connected = ko.observable(false);
        self.targetInput = ko.observable(0);
        self.observeOnly = ko.observable(true);

        // Latest firmware version fetched from BTT docs by the backend at startup.
        self.latestFwVersion = ko.observable(null);
        self.latestFwVersionUrl = ko.observable(null);

        // Firmware-reported extras (null = not yet observed).
        self.fwVersion = ko.observable(null);
        // The Firmware row in the Settings dialog lives in the
        // SettingsViewModel binding scope, so we update it via plain DOM
        // instead of fighting with cross-VM Knockout bindings.
        self.fwVersion.subscribe(function (value) {
            var el = document.getElementById("pandabreath_fwversion_inline");
            if (el) el.textContent = value || gettext("not reported");
            self._updateSettingsFwBadge();
        });
        self.latestFwVersion.subscribe(function () {
            self._updateSettingsFwBadge();
        });

        // Strip leading "V"/"v" for comparison so "1.0.3" == "V1.0.3".
        // Exposed on self so Knockout data-bind expressions can call it.
        var _normFw = function (v) {
            return v ? v.replace(/^[Vv]/, "") : "";
        };
        self._normFw = _normFw;

        self._updateSettingsFwBadge = function () {
            var badge = document.getElementById("pandabreath_fw_update_badge");
            if (!badge) return;
            var current = self.fwVersion();
            var latest = self.latestFwVersion();
            if (current && latest && _normFw(current) !== _normFw(latest)) {
                badge.textContent = latest + gettext(" available");
                badge.href = self.latestFwVersionUrl() || "#";
                badge.style.display = "inline-block";
            } else {
                badge.style.display = "none";
            }
        };
        self.dryTarget = ko.observable(null);
        self.dryTimerHours = ko.observable(null);
        self.dryRemainingS = ko.observable(null);
        self.bedTempLimit = ko.observable(null);
        self.filterThreshold = ko.observable(null);
        self.isRunning = ko.observable(null);
        self.printerType = ko.observable(null);
        self.printerState = ko.observable(null);

        // MQTT bridge state (firmware V1.0.4+). The settings template is
        // bound to the SettingsViewModel (custom_bindings: False), so these
        // dynamic bits are driven by direct DOM access from here — same
        // approach as the firmware-version row. mqttSupported gates the
        // toggle; mqttDeviceBroker holds the broker the device itself
        // reports (from the WS `ha` block) for the auto-prefill.
        self.mqttSupported = ko.observable(false);
        self.mqttActive = ko.observable(false);
        self.mqttDeviceBroker = ko.observable(null); // {ip,port,user} | null

        self._updateMqttSettingsDom = function () {
            var supported = self.mqttSupported();
            var cb = document.getElementById("pandabreath_mqtt_enabled");
            if (cb) cb.disabled = !supported;
            var warn = document.getElementById("pandabreath_mqtt_unsupported");
            if (warn) warn.style.display = supported ? "none" : "";
            var hintEl = document.getElementById(
                "pandabreath_mqtt_broker_hint",
            );
            if (hintEl) {
                var b = self.mqttDeviceBroker();
                if (b && b.ip) {
                    hintEl.innerHTML =
                        "Device is bound to broker <code>" +
                        b.ip +
                        ":" +
                        (b.port || 1883) +
                        "</code>.";
                    hintEl.style.display = "inline-block";
                } else {
                    hintEl.style.display = "none";
                }
            }
            var btn = document.getElementById("pandabreath_mqtt_prefill");
            if (btn) btn.disabled = !self.mqttDeviceBroker();
        };
        self.mqttSupported.subscribe(self._updateMqttSettingsDom);
        self.mqttDeviceBroker.subscribe(self._updateMqttSettingsDom);

        var pbSettings = function () {
            var s =
                self.settings &&
                self.settings.settings &&
                self.settings.settings.plugins &&
                self.settings.settings.plugins.pandabreath;
            return s || null;
        };
        var readPbSetting = function (name) {
            var s = pbSettings();
            if (!s || !s[name]) return null;
            return ko.unwrap(s[name]);
        };
        self.mqttSupportDisplay = ko.pureComputed(function () {
            if (!self.fwVersion()) return gettext("waiting for device status");
            return self.mqttSupported()
                ? gettext("supported")
                : gettext("requires firmware V1.0.4+");
        });
        self.mqttBridgeStateDisplay = ko.pureComputed(function () {
            if (!readPbSetting("mqtt_enabled")) return gettext("disabled");
            return self.mqttActive()
                ? gettext("active")
                : gettext("enabled, waiting");
        });
        self.mqttSupportBadgeCss = ko.pureComputed(function () {
            return {
                "label-success": self.mqttSupported(),
                "label-warning": !self.mqttSupported() && !!self.fwVersion(),
                "label-info": !self.fwVersion(),
            };
        });
        self.mqttBridgeBadgeCss = ko.pureComputed(function () {
            var enabled = !!readPbSetting("mqtt_enabled");
            return {
                "label-success": enabled && self.mqttActive(),
                "label-warning": enabled && !self.mqttActive(),
                "label-info": !enabled,
            };
        });
        self.mqttConfiguredBrokerDisplay = ko.pureComputed(function () {
            var host = readPbSetting("mqtt_host");
            var port = readPbSetting("mqtt_port");
            if (!host) return "—";
            var normalizedPort = port || 1883;
            return host + ":" + normalizedPort;
        });
        self.mqttBaseTopicDisplay = ko.pureComputed(function () {
            return readPbSetting("mqtt_base_topic") || "octoprint/pandabreath";
        });
        self.mqttDeviceBrokerDisplay = ko.pureComputed(function () {
            var b = self.mqttDeviceBroker();
            if (!b || !b.ip) return "—";
            return b.ip + ":" + (b.port || 1883);
        });

        // Recent command-acknowledgement frames from the device.
        // Each entry: { ts: epoch_seconds, type: str, ok: 0|1|null }.
        self.responses = ko.observableArray([]);

        // Catch-all diagnostics dict pushed by the controller. Keys we
        // know about: language, net_sta_ip, net_sta_hostname,
        // net_sta_state, net_ap_ssid, net_ap_ip, net_ap_on,
        // net_wifi_ssid, printer_name, printer_host, printer_port,
        // printer_scan, printer_list.
        self.diagnostics = ko.observable({});
        var diagField = function (key) {
            return ko.pureComputed(function () {
                var v = self.diagnostics()[key];
                if (v === null || v === undefined || v === "") return "—";
                return v;
            });
        };
        self.languageDisplay = diagField("language");
        self.netStaIpDisplay = diagField("net_sta_ip");
        self.netStaHostnameDisplay = diagField("net_sta_hostname");
        self.netStaStateDisplay = ko.pureComputed(function () {
            var v = self.diagnostics().net_sta_state;
            if (v === null || v === undefined) return "—";
            // Common ESP-IDF station states: 3=connected, 2=connecting,
            // 0=idle. Surface raw + label when known.
            var labels = { 0: "idle", 2: "connecting", 3: "connected" };
            return labels[v] ? v + " (" + labels[v] + ")" : String(v);
        });
        self.netApOnDisplay = ko.pureComputed(function () {
            var v = self.diagnostics().net_ap_on;
            if (v === null || v === undefined) return "—";
            return v ? gettext("on") : gettext("off");
        });
        self.netApSsidDisplay = diagField("net_ap_ssid");
        self.netApIpDisplay = diagField("net_ap_ip");
        self.netWifiSsidDisplay = diagField("net_wifi_ssid");
        self.pairedPrinterDisplay = ko.pureComputed(function () {
            var d = self.diagnostics();
            if (!d.printer_name && !d.printer_host) return "—";
            var hostport =
                (d.printer_host || "?") +
                (d.printer_port ? ":" + d.printer_port : "");
            return (d.printer_name || "?") + " @ " + hostport;
        });
        // Mirror the bound-printer summary into the Settings dialog the
        // same way fwVersion does — the Settings template runs under the
        // SettingsViewModel binding scope, so plain DOM is simpler than
        // wiring a second Knockout root.
        self.pairedPrinterDisplay.subscribe(function (value) {
            var el = document.getElementById(
                "pandabreath_paired_printer_inline",
            );
            if (el) el.textContent = value || gettext("not bound");
        });
        // True when we actually have pairing info from the device.
        self.pairedPrinterKnown = ko.pureComputed(function () {
            var d = self.diagnostics();
            return !!(d.printer_name || d.printer_host);
        });
        // Short name for the bound printer — used in the compact sidebar
        // where the host:port is too noisy.
        self.pairedPrinterName = ko.pureComputed(function () {
            var d = self.diagnostics();
            return d.printer_name || d.printer_host || "—";
        });
        // Sidebar-friendly status text without the printer name —
        // 'reachable' renamed to 'bound' since "reachable" implies an
        // active connection to us, which it isn't (the Panda Breath binds
        // to the printer, not to OctoPrint).
        self.pairedPrinterSidebarStatus = ko.pureComputed(function () {
            var v = self.printerState();
            if (v === null || v === undefined) return gettext("unknown");
            if (v === 2) return gettext("binding");
            if (v === 3) return gettext("bound");
            if (v === 4) return gettext("unreachable");
            return gettext("state ") + v;
        });
        // CSS class for the paired-printer status badge — derived from
        // the same printer_state mapping we use in Diagnostics.
        self.pairedPrinterBadgeCss = ko.pureComputed(function () {
            var v = self.printerState();
            if (v === 3) return "label label-success";
            if (v === 2) return "label label-info";
            if (v === 4) return "label label-important";
            return "label";
        });
        self.pairedPrinterBadgeText = ko.pureComputed(function () {
            var v = self.printerState();
            if (v === null || v === undefined) return gettext("unknown");
            if (v === 3) return gettext("reachable");
            if (v === 2) return gettext("binding");
            if (v === 4) return gettext("unreachable");
            return gettext("state ") + v;
        });
        self.printerScanList = ko.pureComputed(function () {
            return self.diagnostics().printer_list || [];
        });
        // printer.scan is a phase indicator emitted by the device:
        //   0 = idle (no scan running)
        //   1 = scan in progress (can take 2-3 minutes on real hardware)
        //   2 = scan complete (list is fresh)
        // Surface as a labelled badge so the user knows whether a click
        // on "Scan for printers" is still pending.
        self.printerScanRaw = ko.pureComputed(function () {
            return self.diagnostics().printer_scan;
        });
        self.printerScanDisplay = ko.pureComputed(function () {
            var v = self.printerScanRaw();
            if (v === null || v === undefined) return "—";
            var labels = {
                0: gettext("idle"),
                1: gettext("scanning…"),
                2: gettext("complete"),
            };
            var label = labels[v];
            return label ? v + " (" + label + ")" : String(v);
        });
        self.printerScanBadgeCss = ko.pureComputed(function () {
            var v = self.printerScanRaw();
            if (v === 1) return "label label-warning";
            if (v === 2) return "label label-success";
            if (v === 0) return "label";
            return "label";
        });

        // Dry-mode user inputs (mirror of dryTarget / dryTimerHours, seeded
        // once on first snapshot — same pattern as targetInput).
        self.dryTargetInput = ko.observable(0);
        self.dryTimerInput = ko.observable(0);
        self.dryTargetInputSeeded = false;
        self.dryTimerInputSeeded = false;
        // Which preset is currently selected in the UI. Default 'custom'
        // so the editable target/timer fields are shown by default; the
        // PLA / PETG buttons set this directly when clicked.
        // NB: the device does not echo filament_drying_mode in its
        // status frames, so this reflects the last selection made
        // *through the plugin* — changes via the Panda Breath WebUI are not
        // visible here until a reconnect won't help either.
        self.dryPreset = ko.observable("custom");
        // Human-readable preset name for the sidebar summary.
        self.dryPresetLabel = ko.pureComputed(function () {
            var p = self.dryPreset();
            if (p === "pla") return "PLA";
            if (p === "petg") return "PETG / ABS";
            return gettext("Custom");
        });

        // Auto-mode threshold inputs: seeded once from the device's
        // current snapshot, same pattern as targetInput.
        self.filterThresholdInput = ko.observable(0);
        self.heaterThresholdInput = ko.observable(0);
        self.filterThresholdInputSeeded = false;
        self.heaterThresholdInputSeeded = false;

        // Temperature history. Each entry: [epoch_ms, chamber, target].
        self.history = ko.observableArray([]);

        // Reflects the debug_panel_enabled setting; the Debug sub-tab in
        // the plugin tab is bound to this so toggling the setting hides
        // the panel and gates frame-broadcast consumption without an
        // OctoPrint reload.
        self.debugPanelEnabled = ko.pureComputed(function () {
            var s = self.settings.settings;
            if (!s || !s.plugins || !s.plugins.pandabreath) return false;
            var flag = s.plugins.pandabreath.debug_panel_enabled;
            return ko.unwrap(flag) ? true : false;
        });
        // Frame entries: { ts: epoch_seconds, dir: "rx"|"tx", frame: str,
        //                  tsDisplay: HH:MM:SS.mmm }.
        self.frames = ko.observableArray([]);
        var MAX_UI_FRAMES = 50;

        // Persistent on-disk frame-log status, populated from the
        // /plugin/pandabreath/frame_logs endpoint.
        self.frameLogEnabled = ko.observable(false);
        self.frameLogDir = ko.observable("");
        self.frameLogFiles = ko.observableArray([]);

        var formatTs = function (ts) {
            var d = new Date(ts * 1000);
            var pad = function (n, w) {
                n = String(n);
                while (n.length < (w || 2)) n = "0" + n;
                return n;
            };
            return (
                pad(d.getHours()) +
                ":" +
                pad(d.getMinutes()) +
                ":" +
                pad(d.getSeconds()) +
                "." +
                pad(d.getMilliseconds(), 3)
            );
        };
        var withDisplay = function (entry) {
            return {
                ts: entry.ts,
                dir: entry.dir,
                frame: entry.frame,
                tsDisplay: formatTs(entry.ts),
            };
        };

        // True while we're in the post-write reconnect window. Locks the
        // apply-buttons so the user can't fire another write into a
        // half-closed socket. Set by post(), cleared on the next
        // 'connected' status push.
        self.reconnectPending = ko.observable(false);

        self.controlsEnabled = ko.pureComputed(function () {
            return (
                !self.locked() &&
                !self.observeOnly() &&
                !self.reconnectPending()
            );
        });
        // Heater-OFF is a write frame, so it stays disabled under
        // observe-only even though it is otherwise lock-bypass-safe.
        // The reconnect-pending gate applies here too.
        self.heaterOffEnabled = ko.pureComputed(function () {
            return !self.observeOnly() && !self.reconnectPending();
        });

        var fmt = function (value, unit) {
            if (value === null || value === undefined) return "—";
            return value.toFixed(1) + (unit || "");
        };

        self.chamberTempDisplay = ko.pureComputed(function () {
            return fmt(self.chamberTemp(), " °C");
        });
        self.targetTempDisplay = ko.pureComputed(function () {
            return fmt(self.targetTemp(), " °C");
        });
        self.heaterState = ko.pureComputed(function () {
            return self.heaterOn() ? "ON" : "OFF";
        });
        self.dryTargetDisplay = ko.pureComputed(function () {
            return fmt(self.dryTarget(), " °C");
        });
        self.dryRemainingDisplay = ko.pureComputed(function () {
            var s = self.dryRemainingS();
            if (s === null || s === undefined) return "—";
            if (s < 0) s = 0;
            var h = Math.floor(s / 3600);
            var m = Math.floor((s % 3600) / 60);
            var sec = Math.floor(s % 60);
            var pad = function (n) {
                return n < 10 ? "0" + n : "" + n;
            };
            return h + "h " + pad(m) + "m " + pad(sec) + "s";
        });
        var intDisplay = function (obs, unit) {
            return ko.pureComputed(function () {
                var v = obs();
                if (v === null || v === undefined) return "—";
                return v + (unit || "");
            });
        };
        self.bedTempLimitDisplay = intDisplay(self.bedTempLimit, " °C");
        self.filterTempDisplay = intDisplay(self.filterThreshold, " °C");
        self.printerTypeDisplay = intDisplay(self.printerType, "");
        // Mapping refined from successive real-hardware Panda captures:
        //   2 — binding attempt in progress (transient)
        //   3 — connected / idle (steady state when paired printer is up)
        //   4 — binding error / paired printer unreachable
        // Other codes are not observed yet; surface the raw number when
        // no label matches so power-users can spot novel states.
        self.printerStateDisplay = ko.pureComputed(function () {
            var v = self.printerState();
            if (v === null || v === undefined) return "—";
            var labels = {
                2: gettext("binding"),
                3: gettext("connected / idle"),
                4: gettext("unreachable / error"),
            };
            var label = labels[v];
            return label ? v + " (" + label + ")" : String(v);
        });
        self.isRunningDisplay = ko.pureComputed(function () {
            var v = self.isRunning();
            if (v === null || v === undefined) return "—";
            return v ? "yes" : "no";
        });
        // Heater shows three states: heating-now (isrunning=1), on but
        // idle (work_on=1, isrunning=0 — chamber at target or set_temp=0),
        // and off (work_on=0).
        self.heaterStateDetailed = ko.pureComputed(function () {
            if (!self.heaterOn()) return gettext("OFF");
            if (self.isRunning()) return gettext("HEATING");
            return gettext("ON (idle)");
        });
        self.heaterHeatingNow = ko.pureComputed(function () {
            return !!self.heaterOn() && !!self.isRunning();
        });

        // Focus tracking for the editable inputs. While the user is
        // actively typing in a field we leave the value alone — once
        // focus is gone we accept backend snapshots as the authoritative
        // value (so refresh / WebUI changes propagate).
        self.targetInputFocused = ko.observable(false);
        self.heaterThresholdInputFocused = ko.observable(false);
        self.filterThresholdInputFocused = ko.observable(false);
        self.dryTargetInputFocused = ko.observable(false);
        self.dryTimerInputFocused = ko.observable(false);

        // applyState is called from both the 2 Hz status push and the
        // explicit REST refresh. To stop edit fields from jittering on
        // every push, we only sync the editor inputs in the "authoritative"
        // refresh path (REST GET via self.refresh) — invoked manually
        // by the user or by autoRefresh after a write.
        self.applyState = function (state, opts) {
            if (!state) return;
            var syncInputs = !!(opts && opts.syncInputs);
            if ("chamber_temp" in state) self.chamberTemp(state.chamber_temp);
            if ("target_temp" in state) {
                self.targetTemp(state.target_temp);
                if (syncInputs && !self.targetInputFocused()) {
                    self.targetInput(state.target_temp);
                }
            }
            if ("heater_on" in state) self.heaterOn(!!state.heater_on);
            if ("mode" in state) self.mode(state.mode);
            if ("locked" in state) self.locked(!!state.locked);
            if ("last_safety_reason" in state) {
                self.lockReason(state.last_safety_reason || "");
            }
            if ("connected" in state) {
                var wasConnected = self.connected();
                self.connected(!!state.connected);
                // Edge: reconnect just completed (offline → online again).
                // Release the apply-button lockout that scheduleAutoRefresh
                // set so the user can resume issuing writes.
                if (
                    !wasConnected &&
                    state.connected &&
                    self.reconnectPending()
                ) {
                    self.reconnectPending(false);
                }
            }
            if ("observe_only" in state) self.observeOnly(!!state.observe_only);
            if ("fw_version" in state) self.fwVersion(state.fw_version);
            if ("latest_fw_version" in state && state.latest_fw_version)
                self.latestFwVersion(state.latest_fw_version);
            if ("latest_fw_url" in state && state.latest_fw_url)
                self.latestFwVersionUrl(state.latest_fw_url);
            if ("dry_target" in state) {
                self.dryTarget(state.dry_target);
                if (
                    syncInputs &&
                    state.dry_target != null &&
                    !self.dryTargetInputFocused()
                ) {
                    self.dryTargetInput(state.dry_target);
                }
            }
            if ("dry_timer_hours" in state) {
                self.dryTimerHours(state.dry_timer_hours);
                if (
                    syncInputs &&
                    state.dry_timer_hours != null &&
                    !self.dryTimerInputFocused()
                ) {
                    self.dryTimerInput(state.dry_timer_hours);
                }
            }
            if ("dry_remaining_s" in state) {
                // Device snapshots carry remaining_seconds at ~2 Hz but
                // the value lags our 1 s tick by 1–2 seconds. Naïve
                // resync would make the display jump back occasionally
                // (e.g. tick reaches 4h 59m 23s, snapshot reports
                // 4h 59m 59s, display flickers backwards).
                //
                // Policy: monotonic countdown. Accept the snapshot only
                // when it would lower the displayed value (drift
                // correction) or when the divergence is too large for
                // drift (start of cycle, reconnect, post-stop reset).
                // Never let the snapshot push the timer back up during
                // a running cycle.
                var incoming = state.dry_remaining_s;
                var current = self.dryRemainingS();
                if (
                    current === null ||
                    current === undefined ||
                    incoming === null ||
                    incoming === undefined ||
                    !self.dryingActive() ||
                    incoming < current ||
                    Math.abs(current - incoming) > 120
                ) {
                    self.dryRemainingS(incoming);
                }
            }
            if ("printer_type" in state) self.printerType(state.printer_type);
            if ("printer_state" in state)
                self.printerState(state.printer_state);
            if ("mqtt_supported" in state)
                self.mqttSupported(!!state.mqtt_supported);
            if ("mqtt_active" in state) self.mqttActive(!!state.mqtt_active);
            if ("diagnostics" in state && state.diagnostics) {
                // Merge so a slim status frame doesn't blow away the
                // network/pairing fields we got from the initial pull.
                var merged = $.extend(
                    {},
                    self.diagnostics(),
                    state.diagnostics,
                );
                self.diagnostics(merged);
                // Surface the broker the device itself reports (ha_* keys
                // from the WS snapshot) for the MQTT settings auto-prefill.
                if (merged.ha_ip) {
                    self.mqttDeviceBroker({
                        ip: merged.ha_ip,
                        port: merged.ha_port || 1883,
                        user: merged.ha_user || "",
                    });
                }
            }
            if ("responses" in state && Array.isArray(state.responses)) {
                // The controller maintains the ring; we just decorate
                // each entry with a display-friendly timestamp.
                self.responses(
                    state.responses.map(function (r) {
                        return $.extend({}, r, {
                            tsDisplay: r.ts
                                ? new Date(r.ts * 1000).toLocaleTimeString()
                                : "—",
                            okDisplay:
                                r.ok === 1 ? "OK" : r.ok === 0 ? "FAIL" : "—",
                        });
                    }),
                );
            }
            if ("bed_temp_limit" in state) {
                self.bedTempLimit(state.bed_temp_limit);
                if (
                    syncInputs &&
                    state.bed_temp_limit != null &&
                    !self.heaterThresholdInputFocused()
                ) {
                    self.heaterThresholdInput(state.bed_temp_limit);
                }
            }
            if ("filter_threshold" in state) {
                self.filterThreshold(state.filter_threshold);
                if (
                    syncInputs &&
                    state.filter_threshold != null &&
                    !self.filterThresholdInputFocused()
                ) {
                    self.filterThresholdInput(state.filter_threshold);
                }
            }
            if ("is_running" in state) self.isRunning(state.is_running);
            if ("history" in state && Array.isArray(state.history)) {
                self.history(state.history);
                self.renderChart();
            }
            if ("frames" in state && Array.isArray(state.frames)) {
                self.frames(state.frames.map(withDisplay));
            }
            if ("frame_log" in state && state.frame_log) {
                self.applyFrameLogStatus(state.frame_log);
            }
        };

        // ---- frame log ----
        var fmtBytes = function (bytes) {
            if (bytes < 1024) return bytes + " B";
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
            return (bytes / 1024 / 1024).toFixed(2) + " MB";
        };
        var fmtMtime = function (epoch) {
            var d = new Date(epoch * 1000);
            return d.toLocaleString();
        };
        var decorateFile = function (entry) {
            return {
                name: entry.name,
                size: entry.size,
                mtime: entry.mtime,
                sizeDisplay: fmtBytes(entry.size),
                mtimeDisplay: fmtMtime(entry.mtime),
                downloadUrl:
                    "plugin/pandabreath/frame_logs/" +
                    encodeURIComponent(entry.name),
            };
        };
        self.applyFrameLogStatus = function (status) {
            self.frameLogEnabled(!!status.enabled);
            self.frameLogDir(status.directory || "");
            var files = (status.files || []).map(decorateFile);
            self.frameLogFiles(files);
        };
        self.refreshFrameLog = function () {
            $.get("plugin/pandabreath/frame_logs").done(
                self.applyFrameLogStatus,
            );
        };
        self.deleteFrameLog = function (file) {
            if (!window.confirm(gettext("Delete ") + file.name + "?")) return;
            $.ajax({
                url:
                    "plugin/pandabreath/frame_logs/" +
                    encodeURIComponent(file.name),
                method: "DELETE",
            }).done(self.applyFrameLogStatus);
        };

        // Fill the MQTT broker settings from the broker the device itself
        // reports (WS `ha` block). Password is never auto-filled — the
        // device redacts it, so the user must (re-)enter it.
        self.prefillMqttFromDevice = function () {
            var b = self.mqttDeviceBroker();
            var s =
                self.settings &&
                self.settings.settings &&
                self.settings.settings.plugins &&
                self.settings.settings.plugins.pandabreath;
            if (!b || !b.ip || !s) return;
            s.mqtt_host(b.ip);
            if (b.port) s.mqtt_port(b.port);
            if (b.user) s.mqtt_username(b.user);
        };

        self.refresh = function () {
            var withDebug = self.debugPanelEnabled();
            var url = "plugin/pandabreath";
            if (withDebug) url += "?debug=1";
            $.get(
                OctoPrint.getSimpleApiUrl
                    ? OctoPrint.getSimpleApiUrl("pandabreath") +
                          (withDebug ? "?debug=1" : "")
                    : "/api/" + url,
            ).done(function (data) {
                self.applyState(data, { syncInputs: true });
            });
        };

        self.onStartupComplete = function () {
            self.refresh();
        };

        // Settings dialog just opened: its DOM now exists, so sync the
        // MQTT gate/hint/prefill button to current state. Also pull a fresh
        // snapshot so fw_version (and the ha block) are current.
        self.onSettingsShown = function () {
            self.refresh();
            self._updateMqttSettingsDom();
        };

        // OctoPrint fires onAfterTabChange when the user switches tabs.
        // Re-render the chart so Flot picks up the now-visible canvas
        // dimensions (it can't measure 0x0 elements).
        self.onAfterTabChange = function (current) {
            if (current === "#tab_plugin_pandabreath") {
                self.renderChart();
            }
        };

        // The chart sits inside the Overview sub-tab. Re-render whenever
        // that pane is shown so Flot has correct dimensions after the
        // user switches back from another sub-tab.
        $(document).on(
            "shown",
            'a[href="#pandabreath_sub_status"]',
            function () {
                self.renderChart();
            },
        );
        // Pull the frame-log file list when the user opens the Debug tab,
        // so it reflects new daily files without a full status refresh.
        $(document).on(
            "shown",
            'a[href="#pandabreath_sub_debug"]',
            function () {
                self.refreshFrameLog();
            },
        );

        // Cap on client-side incremental history. Matches the backend
        // ring size — older samples fall off the front as new ones arrive.
        var MAX_UI_HISTORY = 360;

        self.onDataUpdaterPluginMessage = function (plugin, message) {
            if (plugin !== "pandabreath" || !message) return;
            if (message.kind === "status") {
                var snap = message.snapshot || {};
                self.applyState(snap);
                // Push messages don't carry the full history (would be
                // wasteful per tick). Append a single sample from this
                // snapshot when we have a fresh chamber reading.
                if (
                    snap.chamber_temp !== null &&
                    snap.chamber_temp !== undefined
                ) {
                    var arr = self.history();
                    arr.push([
                        Date.now() / 1000,
                        snap.chamber_temp,
                        snap.target_temp || 0,
                    ]);
                    if (arr.length > MAX_UI_HISTORY) {
                        arr = arr.slice(-MAX_UI_HISTORY);
                    }
                    self.history(arr);
                }
            } else if (message.kind === "latest_fw") {
                if (message.latest_fw_version)
                    self.latestFwVersion(message.latest_fw_version);
                if (message.latest_fw_url)
                    self.latestFwVersionUrl(message.latest_fw_url);
            } else if (message.kind === "frame") {
                if (!self.debugPanelEnabled()) return;
                var entry = withDisplay({
                    ts: message.ts,
                    dir: message.dir,
                    frame: message.frame,
                });
                var arr = self.frames();
                arr.push(entry);
                if (arr.length > MAX_UI_FRAMES) arr = arr.slice(-MAX_UI_FRAMES);
                self.frames(arr);
            } else {
                // Older snapshot format: bare snapshot object.
                self.applyState(message);
            }
        };

        var post = function (command, payload, opts) {
            opts = opts || {};
            OctoPrint.simpleApiCommand("pandabreath", command, payload || {})
                .done(function (data) {
                    self.applyState(data);
                    if (opts.autoRefresh) self.scheduleAutoRefresh();
                })
                .fail(function (xhr) {
                    // Invalid user inputs (range/type) are rejected by the
                    // API with 4xx. Revert edit fields to the last known
                    // valid controller state so stale invalid values do not
                    // stay in the form after the toast is shown.
                    self.targetInputFocused(false);
                    self.heaterThresholdInputFocused(false);
                    self.filterThresholdInputFocused(false);
                    self.dryTargetInputFocused(false);
                    self.dryTimerInputFocused(false);
                    if (
                        self.targetTemp() !== null &&
                        self.targetTemp() !== undefined
                    ) {
                        self.targetInput(self.targetTemp());
                    }
                    if (
                        self.filterThreshold() !== null &&
                        self.filterThreshold() !== undefined
                    ) {
                        self.filterThresholdInput(self.filterThreshold());
                    }
                    if (
                        self.bedTempLimit() !== null &&
                        self.bedTempLimit() !== undefined
                    ) {
                        self.heaterThresholdInput(self.bedTempLimit());
                    }
                    if (
                        self.dryTarget() !== null &&
                        self.dryTarget() !== undefined
                    ) {
                        self.dryTargetInput(self.dryTarget());
                    }
                    if (
                        self.dryTimerHours() !== null &&
                        self.dryTimerHours() !== undefined
                    ) {
                        self.dryTimerInput(self.dryTimerHours());
                    }
                    new PNotify({
                        title: "Panda Breath",
                        text: self._errorText(xhr),
                        type: "error",
                    });
                });
        };

        // Pull a human-readable message out of a failed request. The API
        // replies with a JSON body ({"error": "..."}); show just that
        // string instead of dumping the raw JSON into the toast. Falls
        // back to the raw text, then to a generic message.
        self._errorText = function (xhr) {
            var raw = xhr && xhr.responseText;
            if (raw) {
                try {
                    var parsed = JSON.parse(raw);
                    if (parsed && parsed.error) return parsed.error;
                } catch (e) {
                    // Not JSON — fall through to the raw text.
                }
                return raw;
            }
            return gettext("Request failed");
        };

        // Force a reconnect ~1 s after a successful write so the device's
        // post-write state is reflected in the UI (the firmware doesn't
        // echo writes on the live session; only a fresh connect re-sends
        // the full settings payload). The button lockout is driven by
        // reconnectPending — set here, cleared when the next 'connected'
        // status push arrives, with a safety timeout as a backstop.
        self.scheduleAutoRefresh = function () {
            // With active MQTT control there is no write-induced WS reset,
            // and forcing one defeats the point of the MQTT path. Keep a
            // lightweight REST refresh as a backstop for UI sync only.
            if (self.mqttActive()) {
                setTimeout(function () {
                    self.refresh();
                }, 1500);
                return;
            }
            if (self.reconnectPending()) return;
            self.reconnectPending(true);
            setTimeout(function () {
                OctoPrint.simpleApiCommand(
                    "pandabreath",
                    "refresh_settings",
                    {},
                );
            }, 1000);
            // After the reconnect has had time to land a fresh snapshot
            // in the controller, pull it via REST with syncInputs=true
            // so the editor fields take the device's authoritative
            // post-write value instead of whatever the user just typed.
            setTimeout(function () {
                self.refresh();
            }, 8000);
            // Backstop in case the reconnect never produces a 'connected'
            // push (network swallow, adapter idle, etc.) — release the
            // lockout after a generous window so the user is never stuck.
            setTimeout(function () {
                if (self.reconnectPending()) self.reconnectPending(false);
            }, 15000);
        };

        self.applyTarget = function () {
            post(
                "set_target",
                { value: parseFloat(self.targetInput()) },
                { autoRefresh: true },
            );
        };
        // Combined Custom apply — sends target + timer + commit in one
        // transaction so the user only pays one reconnect cycle.
        self.applyCustomDry = function () {
            post(
                "set_custom_dry",
                {
                    value: parseFloat(self.dryTargetInput()),
                    hours: parseInt(self.dryTimerInput(), 10),
                },
                { autoRefresh: true },
            );
        };
        self.selectPresetPla = function () {
            self.dryPreset("pla");
            post("preset_pla", {}, { autoRefresh: true });
        };
        self.selectPresetPetg = function () {
            self.dryPreset("petg");
            post("preset_petg", {}, { autoRefresh: true });
        };
        self.applyFilterThreshold = function () {
            post(
                "set_filter_threshold",
                { value: parseFloat(self.filterThresholdInput()) },
                { autoRefresh: true },
            );
        };
        self.applyHeaterThreshold = function () {
            post(
                "set_heater_threshold",
                { value: parseFloat(self.heaterThresholdInput()) },
                { autoRefresh: true },
            );
        };
        self.startDrying = function () {
            post("start_drying", {}, { autoRefresh: true });
        };
        self.stopDrying = function () {
            post("stop_drying", {}, { autoRefresh: true });
        };
        // True when the device is actively drying — surfaces from the
        // isrunning flag in the snapshot.
        self.dryingActive = ko.pureComputed(function () {
            return self.mode() === "dry" && !!self.isRunning();
        });
        // The device only accepts ``isrunning:1`` when work_mode is dry
        // and work_on is true. The plugin must mirror those preconditions
        // — otherwise the button silently sends a frame the firmware
        // discards.
        self.canStartDrying = ko.pureComputed(function () {
            return (
                self.controlsEnabled() &&
                !self.octoprintBusy() &&
                !self.dryingActive() &&
                self.mode() === "dry" &&
                !!self.heaterOn()
            );
        });
        self.canStopDrying = ko.pureComputed(function () {
            return self.controlsEnabled() && self.dryingActive();
        });
        // Convenience gate for any dry-mode write: presets, custom inputs
        // and Apply Custom are only meaningful when the device is in dry
        // mode and not already running a cycle. The firmware ignores
        // dry writes outside dry mode and would reset the remaining
        // timer if commit fired during a running cycle.
        self.dryEditEnabled = ko.pureComputed(function () {
            return (
                self.controlsEnabled() &&
                !self.octoprintBusy() &&
                self.mode() === "dry" &&
                !self.dryingActive()
            );
        });
        // Gate the Dry-mode button in the Chamber tab: switching the
        // device into dry-mode is the entry point to a dry cycle, so it is
        // blocked while the printer is busy (the backend rejects it too).
        // Auto/Manual stay available — only Dry is filament-drying.
        self.canSelectDryMode = ko.pureComputed(function () {
            return self.controlsEnabled() && !self.octoprintBusy();
        });
        // Client-side 1 s tick that decrements dryRemainingS while a dry
        // cycle is running. Snapshots only arrive on reconnect, so without
        // this the UI would freeze on the last known value. Every fresh
        // snapshot resyncs the observable, so any drift heals on its own.
        window.setInterval(function () {
            if (!self.dryingActive()) return;
            var s = self.dryRemainingS();
            if (s === null || s === undefined || s <= 0) return;
            self.dryRemainingS(s - 1);
        }, 1000);
        self.scanPrinters = function () {
            post("scan_printers");
            new PNotify({
                title: "Panda Breath",
                text: gettext(
                    "Printer scan triggered — refresh in a few seconds.",
                ),
                type: "info",
            });
        };
        self.refreshSettings = function () {
            post("refresh_settings");
            // Pull the fresh snapshot a few seconds after the reconnect
            // has had time to land so the editor inputs catch the
            // device's authoritative current values.
            setTimeout(function () {
                self.refresh();
            }, 8000);
            new PNotify({
                title: "Panda Breath",
                text: gettext(
                    "Forcing a reconnect — sidebar will blip offline " +
                        "briefly while a fresh snapshot is pulled.",
                ),
                type: "info",
            });
        };

        // The Pairing-actions buttons live inside the Settings dialog,
        // which is bound by OctoPrint's SettingsViewModel — Knockout
        // click bindings on $root would point at the wrong VM. Wire the
        // plain DOM click events from here instead. ``one`` so a reopen
        // of the dialog doesn't stack handlers.
        $(document).on("click", "#pandabreath_btn_scan_printers", function () {
            self.scanPrinters();
        });
        $(document).on(
            "click",
            "#pandabreath_btn_refresh_settings",
            function () {
                self.refreshSettings();
            },
        );
        $(document).on("click", "#pandabreath_mqtt_prefill", function () {
            self.prefillMqttFromDevice();
        });
        $(document).on(
            "click",
            "#pandabreath_btn_delete_frame_logs",
            function () {
                if (
                    !window.confirm(
                        gettext("Delete all persistent frame logs?"),
                    )
                ) {
                    return;
                }
                $.ajax({
                    url: "plugin/pandabreath/frame_logs",
                    method: "DELETE",
                })
                    .done(function (data) {
                        if (data && data.status) {
                            self.applyFrameLogStatus(data.status);
                        } else {
                            self.refreshFrameLog();
                        }
                        new PNotify({
                            title: "Panda Breath",
                            text:
                                gettext("Deleted ") +
                                (data.deleted || 0) +
                                gettext(" frame log file(s)."),
                            type: "success",
                        });
                    })
                    .fail(function (xhr) {
                        new PNotify({
                            title: "Panda Breath",
                            text: xhr.responseText || gettext("Delete failed"),
                            type: "error",
                        });
                    });
            },
        );
        // Mirror the frame-log file count into the Settings dialog's
        // summary span, same DOM-subscribe pattern as the firmware row.
        self.frameLogFiles.subscribe(function (files) {
            var el = document.getElementById("pandabreath_framelog_summary");
            if (!el) return;
            if (!files || files.length === 0) {
                el.textContent = gettext("no log files");
            } else {
                el.textContent = files.length + gettext(" file(s)");
            }
        });
        self.setMode = function (mode) {
            post("set_mode", { mode: mode }, { autoRefresh: true });
        };
        self.setHeater = function (on) {
            post("set_heater", { on: !!on }, { autoRefresh: true });
        };
        self.lock = function () {
            post("lock");
        };
        self.unlock = function () {
            post("unlock");
        };

        // ---- chart ----
        // Re-renders the Flot chart from the current self.history(). Safe
        // to call from anywhere: bails out if the tab is not in the DOM
        // (other VMs may push state before the user opens it).
        self.renderChart = function () {
            var el = document.getElementById("pandabreath-chart");
            if (!el || !el.offsetWidth) return;
            var samples = self.history();
            var chamberSeries = [];
            var targetSeries = [];
            for (var i = 0; i < samples.length; i++) {
                var s = samples[i];
                var ts = s[0] * 1000;
                if (s[1] !== null && s[1] !== undefined) {
                    chamberSeries.push([ts, s[1]]);
                }
                if (s[2] !== null && s[2] !== undefined) {
                    targetSeries.push([ts, s[2]]);
                }
            }
            try {
                // Chart series labels use distinct, multi-word msgids
                // so they don't collide with OctoPrint's own translation
                // catalog (where the bare words "Chamber" / "Target"
                // resolve to "Kammer" / "Soll" in German). These strings
                // live in our own translations/messages.pot and are
                // translated separately by the plugin.
                $.plot(
                    el,
                    [
                        {
                            label: gettext("Chamber temperature"),
                            data: chamberSeries,
                            color: "#d9534f",
                            lines: { show: true, lineWidth: 2 },
                        },
                        {
                            label: gettext("Chamber target"),
                            data: targetSeries,
                            color: "#5bc0de",
                            lines: {
                                show: true,
                                lineWidth: 1,
                                dashes: { show: true },
                            },
                        },
                    ],
                    {
                        xaxis: { mode: "time", timezone: "browser" },
                        yaxis: { min: 0 },
                        grid: { borderWidth: 1, hoverable: true },
                        legend: { position: "nw" },
                    },
                );
            } catch (e) {
                // Flot may throw if the element is hidden or sized 0 —
                // a later render call will succeed once the tab is shown.
            }
        };
        // Re-render on every history mutation, throttled by the next
        // animation frame to avoid back-to-back redraws on burst pushes.
        self.history.subscribe(function () {
            if (self._chartFrame) return;
            self._chartFrame = window.requestAnimationFrame(function () {
                self._chartFrame = null;
                self.renderChart();
            });
        });

        self.confirmEmergencyStop = function () {
            var message = gettext(
                "Trigger Panda Breath emergency stop?\n\n" +
                    "This switches the device power off and engages " +
                    "the safety lock — even in observe-only mode. " +
                    "The Panda Breath firmware stops any running dry cycle " +
                    "on its own.",
            );
            if (!window.confirm(message)) return;
            post("emergency_stop");
            new PNotify({
                title: "Panda Breath",
                text: gettext("Emergency stop sent."),
                type: "error",
                hide: false,
            });
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: PandabreathViewModel,
        dependencies: [
            "loginStateViewModel",
            "settingsViewModel",
            "printerStateViewModel",
        ],
        elements: [
            "#tab_plugin_pandabreath",
            "#sidebar_plugin_pandabreath",
            "#navbar_plugin_pandabreath",
            // NB: #settings_plugin_pandabreath is intentionally NOT in
            // this list. Its template uses `custom_bindings: False`, so
            // OctoPrint binds it with the SettingsViewModel; adding it
            // here would cause Knockout's "cannot apply bindings
            // multiple times" error and break status propagation. The
            // few PandaBreath-specific bits in that template
            // (firmware-version row, Scan/Refresh buttons) are wired
            // via direct DOM access from this VM instead.
        ],
    });
});
