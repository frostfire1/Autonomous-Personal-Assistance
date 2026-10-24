"use client";

import { useEffect, useRef, useState } from "react";
import { api, audioWithInfo, put, useApi } from "@/lib/api";

type Prefs = {
  language: string;
  voice: string;
  city: string;
  lat: number | null;
  lon: number | null;
  gps: boolean;
  topics: string[];
  keywords: string[];
  currency: string;
};
type Voice = { key: string; label: string; engine: string; gender: string };
type PrefsView = {
  prefs: Prefs;
  languages: { key: string; label: string }[];
  topics: { key: string; label: string }[];
  currencies?: { key: string; label: string }[];
  voices: Voice[];
};

export default function SettingsPage() {
  const { data: server, error } = useApi<unknown>("/health");
  const { data: view, reload } = useApi<PrefsView>("/prefs");
  const [form, setForm] = useState<Prefs | null>(null);
  const [voices, setVoices] = useState<Voice[]>([]);
  const [city, setCity] = useState("");
  const [words, setWords] = useState("");
  const [msg, setMsg] = useState("");
  const [playing, setPlaying] = useState(false);
  const player = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    if (view) {
      setForm(view.prefs);
      setVoices(view.voices);
      setCity(view.prefs.city);
      setWords(view.prefs.keywords.join(", "));
    }
  }, [view]);

  async function save(change: Partial<Prefs>) {
    setMsg("Saving...");
    try {
      await put<PrefsView>("/prefs", change);
      setMsg("Saved. Applies to the next briefing.");
      reload();
    } catch (e) {
      const m = (e as Error).message;
      setMsg(m === "city not found" ? "City not found." : `Failed to save: ${m}`);
    }
  }

  async function chooseLanguage(language: string) {
    if (!form) return;
    setForm({ ...form, language });
    const list = await api<{ voices: Voice[] }>(`/prefs/voices?lang=${language}`).catch(() => null);
    if (list) setVoices(list.voices);
    save({ language });
  }

  function toggleTopic(key: string, on: boolean) {
    if (!form) return;
    const topics = on ? [...form.topics, key] : form.topics.filter((x) => x !== key);
    setForm({ ...form, topics });
    save({ topics });
  }

  async function sample() {
    if (!form) return;
    setPlaying(true);
    setMsg("Generating voice sample...");
    try {
      const a = await audioWithInfo(`/prefs/preview?voice=${encodeURIComponent(form.voice)}&lang=${form.language}`);
      const el = new Audio(a.url);
      player.current = el;
      el.onended = () => setPlaying(false);
      await el.play();
      setMsg(a.backup ? `This is a backup voice (${a.engine}): your chosen voice is currently unavailable, usually because the free quota ran out. Try again in a few minutes.` : "");
    } catch {
      setPlaying(false);
      setMsg("The voice sample could not be created (the voice service is unavailable).");
    }
  }

  const known = voices.some((v) => v.key === form?.voice);

  return (
    <>
      <h1>Settings</h1>
      <div className="card">
        <div className="muted">Server connection</div>
        <div>{error ? "Not connected" : server ? "Connected" : "Checking..."}</div>
      </div>

      <h2>Morning briefing</h2>
      {!form || !view ? <p className="muted">Loading...</p> : (
        <div className="card">
          <div className="row">
            <label>Language{" "}
              <select value={form.language} onChange={(e) => chooseLanguage(e.target.value)}>
                {view.languages.map((l) => <option key={l.key} value={l.key}>{l.label}</option>)}
              </select>
            </label>
          </div>
          {view.currencies && (
            <div className="row">
              <label>Home currency{" "}
                <select value={form.currency} onChange={(e) => { const currency = e.target.value; setForm({ ...form, currency }); save({ currency }); }}>
                  {view.currencies.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
                </select>
              </label>
            </div>
          )}
          <p className="muted">One ledger, one currency, no conversion. It sets how money is written and which bank-notification amounts are read (a notification in another currency is not recorded). Reload this page after changing it.</p>
          <div className="row">
            <label>Voice{" "}
              <select value={form.voice} onChange={(e) => { const voice = e.target.value; setForm({ ...form, voice }); save({ voice }); }}>
                {!known && <option value={form.voice}>{form.voice}</option>}
                {voices.map((v) => <option key={v.key} value={v.key}>{v.label}</option>)}
              </select>
            </label>
            <button onClick={sample} disabled={playing}>{playing ? "Playing..." : "Play sample"}</button>
          </div>
          <p className="muted">The voice list follows the language and voice engine (Gemini, Microsoft, or a custom provider). Voice models are configured on the AI Models page.</p>

          <h3>News topics</h3>
          <div className="row">
            {view.topics.map((t) => (
              <label key={t.key}>
                <input type="checkbox" checked={form.topics.includes(t.key)} onChange={(e) => toggleTopic(t.key, e.target.checked)} />{" "}
                {t.label}
              </label>
            ))}
          </div>
          <div className="row">
            <label>Followed keywords{" "}
              <input value={words} placeholder="e.g. bitcoin, kubernetes, Telkom" onChange={(e) => setWords(e.target.value)} />
            </label>
            <button
              onClick={() => save({ keywords: words.split(",").map((w) => w.trim()).filter(Boolean) })}
              disabled={words === form.keywords.join(", ")}
            >
              Save keywords
            </button>
          </div>
          <p className="muted">News from any source containing your keywords appears in the &quot;Followed&quot; section. CVEs and their technologies/versions are under the Cyber security and CVE topic.</p>

          <h3>Weather</h3>
          <div className="row">
            <label>City{" "}
              <input value={city} placeholder="e.g. Surabaya" onChange={(e) => setCity(e.target.value)} disabled={form.gps} />
            </label>
            <button onClick={() => save({ city })} disabled={form.gps || city.trim() === form.city}>Save city</button>
          </div>
          <div className="row">
            <label>
              <input type="checkbox" checked={form.gps} onChange={(e) => { const gps = e.target.checked; setForm({ ...form, gps }); save({ gps }); }} />{" "}
              Weather follows the phone's GPS location (the Android app sends a rough position each time it opens)
            </label>
          </div>
          {form.gps && <p className="muted">{form.lat != null ? `Last position received: ${form.lat}, ${form.lon}` : "No position from the phone yet: open the Android app and allow location access."}</p>}
          {msg && <p className="muted">{msg}</p>}
        </div>
      )}

      <h2>Configured elsewhere</h2>
      <ul>
        <li>AI providers, each agent's model, disabled agents, voice engine and model: the AI Models page.</li>
        <li>API key, signing secret, wake-up time and monitored bank apps: the Android app.</li>
        <li>Morning briefing schedule, Google Sheets, Gemini and Firecrawl keys: the <code>/opt/apa/.env</code> file on the server.</li>
        <li>Budgets: type them in Telegram (for example &quot;food budget 1.5 million&quot;); the AI asks for your confirmation first.</li>
      </ul>
      <p className="muted">The choices above are stored on the server, so they are the same in the app and on the web.</p>
    </>
  );
}
