"use client";

import { useState } from "react";
import { del, post, put, useApi } from "@/lib/api";

type Tool = { name: string; description: string; args: string[]; read_only: boolean };
type Server = {
  id: string;
  name: string;
  url: string;
  header: string;
  has_token: boolean;
  has_login: boolean;
  role: string;
  topics: string[];
  enabled: boolean;
  auto: string[];
  off: string[];
  tools: Tool[];
  refreshed_at: number;
};

export default function SkillsPage() {
  const { data, reload } = useApi<{ servers: Server[]; mcp_server_on: boolean }>("/skills");
  const [f, setF] = useState({ name: "", url: "", token: "", header: "Authorization" });
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [login, setLogin] = useState<{ pending: string; auth_url: string; redirect: string } | null>(null);
  const [pasted, setPasted] = useState("");
  const [spec, setSpec] = useState<Record<string, { role: string; topics: string }>>({});

  const PRESETS = {
    health: {
      name: "Health",
      url: "http://localhost:8000/sse",
      role: "You are the owner's personal health & biohacker coach. When they mention sleep, workouts, heart rate, blood pressure, vitals, biomarkers, steps, or lab results, use the health skills to inspect and log data. Offer sharp, caring, actionable biohacking advice.",
      topics: "kesehatan, tidur, biomarker, detak jantung, tensi, denyut, langkah, olahraga, gym, workout, health, sleep, heart rate, blood pressure, vitals, steps, fitness, lab, suplemen, hrv, body temp",
    },
    nutrition: {
      name: "Nutrition",
      url: "http://localhost:8001/sse",
      role: "You are the owner's nutrition and dietary coach. When they mention a meal, drink, water, food, snacks, calories, protein, carbs, fats, sugar, caffeine, or body weight, use the nutrition skills to look foods up, compute macros, and log them. Provide macro breakdowns and encourage their health goals.",
      topics: "makan, minum, sarapan, makan siang, makan malam, camilan, kalori, protein, karbo, lemak, gula, kafein, air putih, berat badan, timbangan, diet, meal, breakfast, lunch, dinner, snack, calories, macros, water, weight, nutrition",
    },
    cloud_nutrition: {
      name: "Nutrition (Cloud)",
      url: "https://nutrition-mcp.com/mcp",
      role: "You are the owner's nutrition coach. When they mention a meal, a drink, water, a weigh-in or a food question, use the nutrition skills: look foods up (search_foods, get_food_macros), then log them. Ask one short question if the portion is unclear. Be encouraging and never shame food choices.",
      topics: "makan, minum, sarapan, makan siang, makan malam, camilan, kalori, protein, karbo, lemak, gula, kafein, air putih, berat badan, diet, meal, breakfast, lunch, dinner, snack, calories, macros, water, weight",
    },
    smart_home: {
      name: "Smart Home",
      url: "http://localhost:8080/mcp",
      role: "You are the home automation assistant. Control connected smart devices, check room temperatures, and monitor home metrics.",
      topics: "lampu, ac, kamar, rumah, suhu, switch, smart home, iot",
    },
  };

  async function run(fn: () => Promise<unknown>, ok: string) {
    setBusy(true);
    try {
      await fn();
      setMsg(ok);
      reload();
    } catch (e) {
      setMsg(`Not done: ${(e as Error).message}`);
    }
    setBusy(false);
  }

  return (
    <>
      <h1>Skills & MCP Servers</h1>
      <p className="muted">
        Connect Model Context Protocol (MCP) servers to equip your AI assistant with dynamic agentic tools.
        Supports both <b>Streamable HTTP</b> (<code>/mcp</code>) and <b>Server-Sent Events</b> (<code>/sse</code>) on remote hosts,
        local networks, and personal home servers. Read-only tools can run instantly; modification tools ask for approval on Telegram
        unless you allow them below.
      </p>

      <div className="card">
        <b>Connect a Server</b>

        <div className="row" style={{ flexWrap: "wrap", gap: 6, margin: "8px 0" }}>
          <span className="muted" style={{ fontSize: 13, alignSelf: "center" }}>Quick Presets:</span>
          <button
            type="button"
            onClick={() => {
              setF({ ...f, name: PRESETS.health.name, url: PRESETS.health.url });
              setMsg("Health Tracker (Personal Server) preset filled in. Click Connect or adjust address/port.");
            }}
          >
            ❤️ Health (Personal Server)
          </button>
          <button
            type="button"
            onClick={() => {
              setF({ ...f, name: PRESETS.nutrition.name, url: PRESETS.nutrition.url });
              setMsg("Nutrition & Macro (Personal Server) preset filled in. Click Connect or adjust address/port.");
            }}
          >
            🥗 Nutrition (Personal Server)
          </button>
          <button
            type="button"
            onClick={() => {
              setF({ ...f, name: PRESETS.cloud_nutrition.name, url: PRESETS.cloud_nutrition.url });
              setMsg("Cloud Nutrition Tracker preset filled in.");
            }}
          >
            ☁️ Cloud Nutrition
          </button>
          <button
            type="button"
            onClick={() => {
              setF({ ...f, name: PRESETS.smart_home.name, url: PRESETS.smart_home.url });
              setMsg("Smart Home / IoT preset filled in.");
            }}
          >
            🏠 Smart Home
          </button>
        </div>

        <div className="row" style={{ flexWrap: "wrap", gap: 4, marginBottom: 8, fontSize: 12 }}>
          <span className="muted" style={{ alignSelf: "center" }}>Address Helpers:</span>
          {["http://localhost", "http://127.0.0.1", "http://192.168.1."].map((h) => (
            <button
              key={h}
              type="button"
              className="link"
              onClick={() => {
                const portPart = f.url.includes(":") && f.url.indexOf(":", 6) > 0 ? f.url.substring(f.url.indexOf(":", 6)) : ":8000/sse";
                setF({ ...f, url: h + portPart });
              }}
            >
              {h}
            </button>
          ))}
          {[":8000/sse", ":8001/sse", ":8000/mcp", ":8001/mcp", ":8080/mcp", ":3001/sse"].map((p) => (
            <button
              key={p}
              type="button"
              className="link"
              onClick={() => {
                const hostPart = f.url
                  ? f.url.includes(":") && f.url.indexOf(":", 6) > 0
                    ? f.url.substring(0, f.url.indexOf(":", 6))
                    : f.url
                  : "http://localhost";
                setF({ ...f, url: hostPart + p });
              }}
            >
              {p}
            </button>
          ))}
        </div>

        <div className="row">
          <input
            placeholder="name (e.g. Health, Nutrition)"
            value={f.name}
            onChange={(e) => setF({ ...f, name: e.target.value })}
          />
          <input
            style={{ flex: 1 }}
            placeholder="address (e.g. http://localhost:8000/sse or https://host/mcp)"
            value={f.url}
            onChange={(e) => setF({ ...f, url: e.target.value })}
          />
        </div>

        <div className="row">
          <input
            placeholder="header (Authorization)"
            value={f.header}
            onChange={(e) => setF({ ...f, header: e.target.value })}
          />
          <input
            style={{ flex: 1 }}
            type="password"
            placeholder="token (optional, stored encrypted, never shown again)"
            value={f.token}
            onChange={(e) => setF({ ...f, token: e.target.value })}
          />
          <button
            disabled={busy || !f.name.trim() || !f.url.trim()}
            onClick={() =>
              run(async () => {
                await post("/skills", f);
                setF({ name: "", url: "", token: "", header: "Authorization" });
              }, "Connected successfully.")
            }
          >
            {busy ? "Connecting..." : "Connect"}
          </button>
        </div>

        <div className="row">
          <button
            disabled={busy || !f.name.trim() || !f.url.trim()}
            onClick={() =>
              run(async () => {
                const r = await post<{ pending?: string; auth_url?: string; redirect?: string }>("/skills/login/start", {
                  name: f.name,
                  url: f.url,
                });
                if (r.pending) setLogin({ pending: r.pending, auth_url: r.auth_url!, redirect: r.redirect! });
              }, "Open the login page below.")
            }
          >
            Connect with OAuth Login
          </button>
        </div>

        {login && (
          <div className="card" style={{ marginTop: 8 }}>
            <ol>
              <li>
                Open{" "}
                <a href={login.auth_url} target="_blank" rel="noreferrer">
                  the login page
                </a>{" "}
                and sign in.
              </li>
              <li>
                The browser then shows an error page. That is expected: copy the <b>whole address</b> from the address
                bar (it starts with <code>{login.redirect}</code>).
              </li>
              <li>Paste it here:</li>
            </ol>
            <input
              style={{ width: "100%" }}
              placeholder={`${login.redirect}?code=...`}
              value={pasted}
              onChange={(e) => setPasted(e.target.value)}
            />
            <button
              disabled={busy || !pasted.includes("code=")}
              onClick={() =>
                run(async () => {
                  await post("/skills/login/finish", { pending: login.pending, redirected_url: pasted });
                  setLogin(null);
                  setPasted("");
                }, "Signed in and connected.")
              }
            >
              Finish
            </button>
          </div>
        )}
        <span className="muted">{msg}</span>
      </div>

      <p className="muted">
        {data?.mcp_server_on
          ? "The other direction is on: your own data is offered read-only to other AI apps at /mcp (same key as this dashboard)."
          : "The other direction (offering your own data to other AI apps at /mcp) is off: set MCP_ENABLED=true in .env to turn it on."}
      </p>

      {!data ? (
        <p className="muted">Loading...</p>
      ) : data.servers.length === 0 ? (
        <p className="muted">No skills connected yet. Use the presets above to connect your Health or Nutrition server.</p>
      ) : (
        data.servers.map((s) => (
          <div className="card" key={s.id} style={{ marginBottom: 8 }}>
            <div className="row" style={{ alignItems: "center" }}>
              <b>{s.name}</b>
              <span
                style={{
                  fontSize: 11,
                  padding: "2px 6px",
                  borderRadius: 4,
                  background: s.url.toLowerCase().includes("/sse") ? "#2563eb22" : "#16a34a22",
                  color: s.url.toLowerCase().includes("/sse") ? "#3b82f6" : "#22c55e",
                  fontWeight: 600,
                }}
              >
                {s.url.toLowerCase().includes("/sse") ? "SSE" : "HTTP"}
              </span>
              <span className="muted" style={{ flex: 1 }}>
                {s.url}
                {s.has_token ? " · token saved" : ""}
                {s.has_login ? " · signed in" : ""}
              </span>
              <button
                onClick={() =>
                  run(
                    () => put(`/skills/${s.id}`, { enabled: !s.enabled }),
                    s.enabled ? "Switched off." : "Switched on."
                  )
                }
              >
                {s.enabled ? "Switch off" : "Switch on"}
              </button>
              <button onClick={() => run(() => post(`/skills/${s.id}/refresh`, {}), "Tools refreshed.")}>
                Refresh tools
              </button>
              <button
                onClick={() =>
                  run(
                    () => put(`/skills/${s.id}`, { auto_all_read: true }),
                    "Allowed all read-only tools without asking."
                  )
                }
                title="Allow all read-only tools without needing manual Telegram confirmation cards"
              >
                ⚡ Allow all read tools
              </button>
              <button
                className="link"
                onClick={() => {
                  if (confirm(`Remove ${s.name}?`)) run(() => del(`/skills/${s.id}`), "Removed.");
                }}
              >
                Remove
              </button>
            </div>

            <details>
              <summary className="muted">
                Specialist Persona: let this skill take charge of the conversation for its topics
              </summary>
              <p className="muted">
                When a message matches one of the topic keywords, the assistant activates this specialist role and
                actively utilizes this server&apos;s tools.
              </p>
              <textarea
                rows={3}
                style={{ width: "100%", marginBottom: 6 }}
                placeholder="Role: how the assistant behaves while it is in charge"
                value={spec[s.id]?.role ?? s.role}
                onChange={(e) =>
                  setSpec({
                    ...spec,
                    [s.id]: { role: e.target.value, topics: spec[s.id]?.topics ?? s.topics.join(", ") },
                  })
                }
              />
              <input
                style={{ width: "100%", marginBottom: 6 }}
                placeholder="topic words, comma separated"
                value={spec[s.id]?.topics ?? s.topics.join(", ")}
                onChange={(e) =>
                  setSpec({
                    ...spec,
                    [s.id]: { role: spec[s.id]?.role ?? s.role, topics: e.target.value },
                  })
                }
              />
              <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
                <button
                  onClick={() =>
                    run(
                      () =>
                        put(`/skills/${s.id}`, {
                          role: spec[s.id]?.role ?? s.role,
                          topics: (spec[s.id]?.topics ?? s.topics.join(", "))
                            .split(",")
                            .map((x) => x.trim())
                            .filter(Boolean),
                        }),
                      "Specialist saved."
                    )
                  }
                >
                  Save Specialist
                </button>
                <button
                  type="button"
                  onClick={() =>
                    setSpec({
                      ...spec,
                      [s.id]: { role: PRESETS.health.role, topics: PRESETS.health.topics },
                    })
                  }
                >
                  Fill Health Coach
                </button>
                <button
                  type="button"
                  onClick={() =>
                    setSpec({
                      ...spec,
                      [s.id]: { role: PRESETS.nutrition.role, topics: PRESETS.nutrition.topics },
                    })
                  }
                >
                  Fill Nutrition Coach
                </button>
              </div>
            </details>

            {s.tools.length === 0 ? (
              <p className="muted">No tools listed. Click Refresh tools to fetch capabilities from the server.</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Tool</th>
                    <th>What it does</th>
                    <th>Runs</th>
                    <th>Allow without asking</th>
                    <th>Off</th>
                  </tr>
                </thead>
                <tbody>
                  {s.tools.map((t) => (
                    <tr key={t.name}>
                      <td>
                        <code>
                          {t.name}({t.args.join(", ")})
                        </code>
                      </td>
                      <td>{t.description}</td>
                      <td>
                        {t.read_only
                          ? "at once (read-only)"
                          : s.auto.includes(t.name)
                          ? "at once (allowed)"
                          : "after you confirm"}
                      </td>
                      <td>
                        <input
                          type="checkbox"
                          checked={s.auto.includes(t.name)}
                          disabled={t.read_only || s.off.includes(t.name)}
                          onChange={(e) =>
                            run(
                              () => put(`/skills/${s.id}`, { tool: t.name, auto: e.target.checked }),
                              "Permission saved."
                            )
                          }
                        />
                      </td>
                      <td>
                        <input
                          type="checkbox"
                          checked={s.off.includes(t.name)}
                          onChange={(e) =>
                            run(
                              () => put(`/skills/${s.id}`, { tool: t.name, off: e.target.checked }),
                              "Toggle saved."
                            )
                          }
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        ))
      )}
    </>
  );
}
