"use client";

import { useState } from "react";
import { post, setKey, useApi } from "@/lib/api";

type DeviceApp = {
  app: string;
  seconds: number;
  category: string;
  formatted: string;
};

type DeviceEntry = {
  id: string;
  device: string;
  kind: "android_phone" | "desktop_tracker";
  os: string;
  label: string;
  status: "online" | "idle" | "offline";
  last_seen_relative: string;
  today_screen_time: string;
  top_apps: DeviceApp[];
  details: Record<string, unknown>;
};

type KeyStatus = {
  active_key: string;
  active_key_masked: string;
  previous_key: string;
  previous_key_masked: string;
  dual_acceptance: boolean;
  rotated_at: string | null;
  is_leaked_sample: boolean;
};

type DevicesResponse = {
  summary: {
    total_devices: number;
    online_devices: number;
    today_total_screen_time_formatted: string;
    dual_acceptance_active: boolean;
  };
  key_status: KeyStatus;
  devices: DeviceEntry[];
  today_top_apps: DeviceApp[];
  downloads: {
    android_apk: string;
    windows_exe: string;
  };
};

export default function DevicesPage() {
  const { data, error, reload } = useApi<DevicesResponse>("/devices", 10_000);
  const [showKey, setShowKey] = useState(false);
  const [copyMsg, setCopyMsg] = useState("");
  const [customKey, setCustomKey] = useState("");
  const [showRotateForm, setShowRotateForm] = useState(false);
  const [actionMsg, setActionMsg] = useState("");
  const [isBusy, setIsBusy] = useState(false);

  if (error) return <p className="muted">Failed to load devices: {error}</p>;
  if (!data) return <p className="muted">Loading devices...</p>;

  const ks = data.key_status;
  const currentKey = ks.active_key;

  async function copyToClipboard(text: string, label: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopyMsg(`Copied ${label}!`);
      setTimeout(() => setCopyMsg(""), 3000);
    } catch {
      setCopyMsg("Failed to copy automatically. Please copy manually.");
    }
  }

  async function handleRotateKey(useCustom: boolean) {
    const confirmMsg = useCustom
      ? `Rotate Ingest Key to custom key? Connected devices will remain connected during the grace period.`
      : `Generate and rotate to a new cryptographically secure Ingest Key? Dual-acceptance grace period will keep existing devices connected.`;

    if (!window.confirm(confirmMsg)) return;

    setIsBusy(true);
    setActionMsg("Rotating key...");
    try {
      const res = await post<{ ok: boolean; active_key: string }>("/devices/rotate-key", {
        new_key: useCustom ? customKey.trim() : null,
      });
      // Update browser session key so Mission Control does not 401
      if (res.active_key) {
        setKey(res.active_key);
      }
      setActionMsg("Key rotated successfully. Dual-acceptance is active.");
      setShowRotateForm(false);
      setCustomKey("");
      reload();
    } catch (e) {
      setActionMsg(`Failed to rotate key: ${(e as Error).message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function handleRevokePreviousKey() {
    if (
      !window.confirm(
        "Revoke previous key? Devices still using the old key will stop syncing until updated with the new key."
      )
    ) {
      return;
    }

    setIsBusy(true);
    setActionMsg("Revoking previous key...");
    try {
      await post<{ ok: boolean }>("/devices/revoke-previous-key", {});
      setActionMsg("Previous key revoked. Only the active key is now accepted.");
      reload();
    } catch (e) {
      setActionMsg(`Failed to revoke key: ${(e as Error).message}`);
    } finally {
      setIsBusy(false);
    }
  }

  const sampleLaptopConfig = {
    server_url: typeof window !== "undefined" ? window.location.origin.replace(/:\d+$/, ":8000") : "http://<SERVER_IP>:8000",
    api_key: currentKey || "<YOUR_INGEST_KEY>",
    device_name: "My Laptop",
    sync_interval_seconds: 30,
    report_active_window: true,
  };

  return (
    <>
      <h1>Device Management & Activity</h1>
      <p className="muted">
        Manage paired Android phones, Windows laptop watchers, track screen time, and rotate ingest credentials safely.
      </p>

      {/* Summary KPI Cards */}
      <div className="grid" style={{ marginBottom: "16px" }}>
        <div className="card">
          <div className="muted">Tracked Devices</div>
          <div className="big">{data.summary.total_devices}</div>
        </div>
        <div className="card">
          <div className="muted">Online Now</div>
          <div className="big">
            <span className={data.summary.online_devices > 0 ? "pill good" : "pill"}>
              ● {data.summary.online_devices} online
            </span>
          </div>
        </div>
        <div className="card">
          <div className="muted">Today's Screen Time</div>
          <div className="big">{data.summary.today_total_screen_time_formatted || "0m"}</div>
        </div>
        <div className="card">
          <div className="muted">Ingest Auth Security</div>
          <div className="big">
            {ks.is_leaked_sample ? (
              <span className="pill bad">⚠️ Leaked Sample</span>
            ) : ks.dual_acceptance ? (
              <span className="pill warn">Dual Acceptance</span>
            ) : (
              <span className="pill good">Active Protected</span>
            )}
          </div>
        </div>
      </div>

      {/* Leaked Sample Warning Alert */}
      {ks.is_leaked_sample && (
        <div
          className="card"
          style={{
            borderLeft: "4px solid var(--bad)",
            background: "rgba(192, 57, 43, 0.08)",
            marginBottom: "16px",
          }}
        >
          <div style={{ fontWeight: 600, color: "var(--bad)" }}>
            ⚠️ Security Alert: Using Public Sample Ingest Key
          </div>
          <p className="muted" style={{ margin: "6px 0 10px" }}>
            Your instance is currently using the public sample key from documentation. Anyone with this key can ingest
            activity or query device status. Please rotate your ingest key below immediately.
          </p>
          <button
            onClick={() => handleRotateKey(false)}
            disabled={isBusy}
            style={{ background: "var(--bad)", color: "#fff", border: 0 }}
          >
            Generate & Rotate Key Now
          </button>
        </div>
      )}

      {/* Dual Acceptance Grace Period Alert */}
      {ks.dual_acceptance && (
        <div
          className="card"
          style={{
            borderLeft: "4px solid var(--warn)",
            background: "rgba(183, 121, 31, 0.08)",
            marginBottom: "16px",
          }}
        >
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap" }}>
            <div>
              <div style={{ fontWeight: 600, color: "var(--warn)" }}>
                🔄 Dual-Key Acceptance Active (Migration Grace Period)
              </div>
              <p className="muted" style={{ margin: "4px 0" }}>
                The server is accepting both the new active key and the previous key ({ks.previous_key_masked}). Once you
                update your Android phone and Laptop config, revoke the old key to conclude rotation.
              </p>
            </div>
            <button
              onClick={handleRevokePreviousKey}
              disabled={isBusy}
              style={{ background: "var(--warn)", color: "#fff", border: 0, marginTop: "6px" }}
            >
              Revoke Previous Key
            </button>
          </div>
        </div>
      )}

      {/* Ingest Key Management Card */}
      <div className="card" style={{ marginBottom: "20px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap" }}>
          <div>
            <div style={{ fontWeight: 600, fontSize: "1.05rem" }}>Ingest API Key</div>
            <div className="muted">Shared secret used by Android APK and Laptop Trackers to send heartbeats and telemetry.</div>
          </div>
          <div className="row" style={{ marginTop: "6px" }}>
            <button className="link" onClick={() => setShowKey(!showKey)}>
              {showKey ? "Hide key" : "Show key"}
            </button>
            <button
              onClick={() => copyToClipboard(currentKey, "Ingest API Key")}
              style={{ padding: "6px 10px" }}
            >
              Copy Key
            </button>
            <button
              onClick={() => setShowRotateForm(!showRotateForm)}
              style={{ padding: "6px 10px", borderColor: "var(--accent)", color: "var(--accent)" }}
            >
              Rotate Key...
            </button>
          </div>
        </div>

        <div style={{ marginTop: "12px", fontFamily: "monospace", fontSize: "0.95rem", background: "var(--bg)", padding: "8px 12px", borderRadius: "6px" }}>
          {showKey ? currentKey : ks.active_key_masked}
        </div>

        {copyMsg && <div className="muted" style={{ marginTop: "6px", color: "var(--good)" }}>✓ {copyMsg}</div>}
        {actionMsg && <div className="muted" style={{ marginTop: "6px" }}>{actionMsg}</div>}

        {/* Rotate Form Drawer */}
        {showRotateForm && (
          <div style={{ marginTop: "16px", paddingTop: "14px", borderTop: "1px solid var(--line)" }}>
            <div style={{ fontWeight: 600, marginBottom: "8px" }}>Rotate Ingest Key Options</div>
            <div className="row">
              <button
                onClick={() => handleRotateKey(false)}
                disabled={isBusy}
                style={{ background: "var(--accent)", color: "#fff", border: 0 }}
              >
                Auto-generate Random 32-char Key
              </button>
              <span className="muted">or enter custom:</span>
              <input
                type="text"
                placeholder="Custom secret key..."
                value={customKey}
                onChange={(e) => setCustomKey(e.target.value)}
                style={{ minWidth: "220px" }}
              />
              <button
                onClick={() => handleRotateKey(true)}
                disabled={isBusy || !customKey.trim()}
              >
                Apply Custom Key
              </button>
              <button className="link" onClick={() => setShowRotateForm(false)}>
                Cancel
              </button>
            </div>
            <div className="muted" style={{ marginTop: "6px", fontSize: "0.82rem" }}>
              💡 Dual-acceptance grace period is enabled automatically. Your devices won't disconnect immediately.
            </div>
          </div>
        )}
      </div>

      {/* Connected Devices Grid */}
      <h2>Connected Devices</h2>
      {data.devices.length === 0 ? (
        <div className="card">
          <p className="muted">No devices have synced yet. Download the tracker or configure your phone below.</p>
        </div>
      ) : (
        <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", marginBottom: "24px" }}>
          {data.devices.map((dev) => (
            <div key={dev.id} className="card">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                <div>
                  <div style={{ fontSize: "1.1rem", fontWeight: 600 }}>
                    {dev.kind === "android_phone" ? "📱" : "💻"} {dev.label}
                  </div>
                  <div className="muted" style={{ fontSize: "0.82rem" }}>
                    {dev.os} • {dev.device}
                  </div>
                </div>
                <span
                  className={
                    dev.status === "online"
                      ? "pill good"
                      : dev.status === "idle"
                      ? "pill warn"
                      : "pill"
                  }
                >
                  ● {dev.status}
                </span>
              </div>

              <div style={{ marginTop: "12px", display: "flex", gap: "16px" }}>
                <div>
                  <div className="muted" style={{ fontSize: "0.78rem" }}>LAST SEEN</div>
                  <div style={{ fontWeight: 550 }}>{dev.last_seen_relative}</div>
                </div>
                <div>
                  <div className="muted" style={{ fontSize: "0.78rem" }}>SCREEN TIME TODAY</div>
                  <div style={{ fontWeight: 550 }}>{dev.today_screen_time}</div>
                </div>
              </div>

              {/* Top Apps on this device */}
              <div style={{ marginTop: "14px" }}>
                <div className="muted" style={{ fontSize: "0.78rem", textTransform: "uppercase", marginBottom: "6px" }}>
                  Top Apps Today
                </div>
                {dev.top_apps && dev.top_apps.length > 0 ? (
                  <div style={{ display: "flex", flexWrap: "wrap", gap: "6px" }}>
                    {dev.top_apps.slice(0, 4).map((a) => (
                      <span
                        key={a.app}
                        className="pill"
                        title={`${a.app}: ${a.formatted} (${a.category})`}
                        style={{ background: "var(--bg)" }}
                      >
                        {a.app.length > 18 ? a.app.slice(0, 16) + "…" : a.app}: <strong>{a.formatted}</strong>
                      </span>
                    ))}
                  </div>
                ) : (
                  <span className="muted" style={{ fontSize: "0.85rem" }}>No app activity recorded today.</span>
                )}
              </div>

              {/* Extra device details if available */}
              {dev.details && Object.keys(dev.details).length > 0 && (
                <div style={{ marginTop: "12px", paddingTop: "8px", borderTop: "1px solid var(--line)", fontSize: "0.82rem" }} className="muted">
                  {typeof dev.details.battery === "number" && (
                    <span>🔋 {dev.details.battery}% {dev.details.charging ? "(charging)" : ""} • </span>
                  )}
                  {typeof dev.details.network === "string" && (
                    <span>📶 {dev.details.network} • </span>
                  )}
                  {typeof dev.details.client_version === "string" && (
                    <span>v{dev.details.client_version}</span>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Setup Guide & Download Cards */}
      <h2>Quick Device Setup</h2>
      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))" }}>
        {/* Windows Laptop Tracker */}
        <div className="card">
          <div style={{ fontWeight: 600, fontSize: "1.05rem", marginBottom: "4px" }}>
            💻 Windows Laptop / Desktop Tracker
          </div>
          <p className="muted" style={{ margin: "4px 0 10px", fontSize: "0.88rem" }}>
            Lightweight background daemon that tracks active foreground windows, sleep/wake status, and reports heartbeats.
          </p>
          <div className="row" style={{ marginBottom: "12px" }}>
            <a
              href={data.downloads.windows_exe}
              download="APA-Tracker.exe"
              style={{
                display: "inline-block",
                background: "var(--accent)",
                color: "#fff",
                padding: "6px 12px",
                borderRadius: "8px",
                textDecoration: "none",
                fontWeight: 500,
                fontSize: "0.9rem",
              }}
            >
              ⬇ Download APA-Tracker.exe
            </a>
          </div>

          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span className="muted" style={{ fontSize: "0.8rem", fontWeight: 600 }}>laptop_config.json</span>
            <button
              className="link"
              onClick={() => copyToClipboard(JSON.stringify(sampleLaptopConfig, null, 2), "laptop_config.json")}
            >
              Copy JSON Config
            </button>
          </div>
          <pre
            style={{
              background: "var(--bg)",
              padding: "10px",
              borderRadius: "6px",
              fontSize: "0.78rem",
              overflowX: "auto",
              maxHeight: "150px",
            }}
          >
            {JSON.stringify(sampleLaptopConfig, null, 2)}
          </pre>
        </div>

        {/* Android Companion App */}
        <div className="card">
          <div style={{ fontWeight: 600, fontSize: "1.05rem", marginBottom: "4px" }}>
            📱 Android Companion App
          </div>
          <p className="muted" style={{ margin: "4px 0 10px", fontSize: "0.88rem" }}>
            Monitors notifications, banking SMS/alerts, health telemetry, and sends real-time heartbeat status.
          </p>
          <div className="row" style={{ marginBottom: "12px" }}>
            <a
              href={data.downloads.android_apk}
              download="latest.apk"
              style={{
                display: "inline-block",
                background: "var(--accent)",
                color: "#fff",
                padding: "6px 12px",
                borderRadius: "8px",
                textDecoration: "none",
                fontWeight: 500,
                fontSize: "0.9rem",
              }}
            >
              ⬇ Download Android APK
            </a>
          </div>
          <div className="muted" style={{ fontSize: "0.84rem", lineHeight: 1.5 }}>
            1. Install APK on your Android device.<br />
            2. Open Settings in the app, paste Server URL:<br />
            <code style={{ background: "var(--bg)", padding: "2px 6px", borderRadius: "4px" }}>
              {sampleLaptopConfig.server_url}
            </code><br />
            3. Enter API Key and enable Notification Access.
          </div>
        </div>
      </div>
    </>
  );
}
