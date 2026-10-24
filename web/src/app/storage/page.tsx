"use client";

import { useEffect, useState } from "react";
import { post, put, useApi } from "@/lib/api";

type Cfg = { backend: "env" | "local" | "s3"; endpoint: string; access_key: string; secret_key_hint: string; bucket: string; region: string; secure: boolean };
type View = { config: Cfg; env_backend: string; env_endpoint: string };

const PRESETS: Record<string, Partial<Cfg>> = {
  garage: { endpoint: "garage:3900", region: "garage", secure: false },
  minio: { endpoint: "minio:9000", region: "", secure: false },
  aws: { endpoint: "s3.amazonaws.com", region: "us-east-1", secure: true },
  r2: { endpoint: "<account>.r2.cloudflarestorage.com", region: "auto", secure: true },
  b2: { endpoint: "s3.us-west-004.backblazeb2.com", region: "us-west-004", secure: true },
};

export default function StoragePage() {
  const { data: view, reload } = useApi<View>("/storage");
  const [cfg, setCfg] = useState<Cfg | null>(null);
  const [secret, setSecret] = useState("");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    if (view) setCfg(view.config);
  }, [view]);

  if (!cfg || !view) return <><h1>Storage</h1><p className="muted">Loading...</p></>;
  const body = () => ({ ...cfg, secret_key: secret === "" ? undefined : secret });

  async function test() {
    setMsg("Testing...");
    try {
      const r = await post<{ ok: boolean; detail: string }>("/storage/test", body());
      setMsg(r.ok ? `Success: ${r.detail}` : `Failed: ${r.detail}`);
    } catch (e) {
      setMsg(`Failed: ${(e as Error).message}`);
    }
  }
  async function save() {
    setMsg("Saving (testing first)...");
    try {
      await put("/storage", body());
      setSecret("");
      setMsg("Saved. New photos will use this storage within a few seconds; old photos remain readable from where they are.");
      reload();
    } catch (e) {
      setMsg(`Not saved: ${(e as Error).message}`);
    }
  }

  return (
    <>
      <h1>Receipt photo storage</h1>
      <p className="muted">Choose where receipt photos are stored. No restart needed: changes apply within a few seconds.</p>
      <div className="card">
        <div className="row">
          {([["env", `Follow the .env file (currently: ${view.env_backend}${view.env_backend !== "local" ? ` at ${view.env_endpoint}` : ""})`], ["local", "This server's disk (easiest, no S3)"], ["s3", "S3 (Garage, MinIO, AWS S3, Cloudflare R2, Backblaze B2, etc.)"]] as const).map(([k, l]) => (
            <label key={k} style={{ display: "block" }}>
              <input type="radio" name="backend" checked={cfg.backend === k} onChange={() => setCfg({ ...cfg, backend: k })} /> {l}
            </label>
          ))}
        </div>
        {cfg.backend === "s3" && (
          <>
            <div className="row">
              Quick fill:
              {Object.keys(PRESETS).map((p) => <button key={p} onClick={() => setCfg({ ...cfg, ...PRESETS[p] })}>{p}</button>)}
            </div>
            <div className="row"><label>Address (host:port){" "}<input value={cfg.endpoint} onChange={(e) => setCfg({ ...cfg, endpoint: e.target.value })} placeholder="e.g. garage:3900" style={{ width: 280 }} /></label></div>
            <div className="row">
              <label>Access key{" "}<input value={cfg.access_key} onChange={(e) => setCfg({ ...cfg, access_key: e.target.value })} /></label>
              <label>Secret key{" "}<input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} placeholder={cfg.secret_key_hint ? `saved (${cfg.secret_key_hint})` : ""} /></label>
            </div>
            <div className="row">
              <label>Bucket{" "}<input value={cfg.bucket} onChange={(e) => setCfg({ ...cfg, bucket: e.target.value })} /></label>
              <label>Region{" "}<input value={cfg.region} onChange={(e) => setCfg({ ...cfg, region: e.target.value })} placeholder="leave empty if unknown" /></label>
              <label><input type="checkbox" checked={cfg.secure} onChange={(e) => setCfg({ ...cfg, secure: e.target.checked })} /> HTTPS</label>
            </div>
          </>
        )}
        <div className="row">
          <button onClick={test}>Test connection</button>
          <button onClick={save}>Save</button>
          <span className="muted">{msg}</span>
        </div>
      </div>
      <p className="muted">The test button creates the bucket if it does not exist, then writes, reads and deletes one small file. Saving only succeeds if the test passes.</p>
    </>
  );
}
