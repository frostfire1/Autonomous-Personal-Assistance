"use client";

import { useCallback, useEffect, useState } from "react";

// Browser-side API helper. Next rewrites /api/* to the FastAPI service. The key is typed once on the login screen and
// kept in this browser only; every request carries it, and a 401 sends the owner back to the login screen.
const KEY = "apa_key";

export function getKey(): string {
  try {
    return localStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

export function setKey(k: string): void {
  try {
    if (k) localStorage.setItem(KEY, k);
    else localStorage.removeItem(KEY);
  } catch {
    /* private window or blocked storage: the login just will not persist */
  }
}

export async function api<T>(path: string, key: string = getKey()): Promise<T> {
  const res = await fetch(`/api${path}`, { headers: { "x-api-key": key } });
  if (res.status === 401) {
    setKey("");
    window.location.reload();
    throw new Error("401");
  }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

/** PUT a JSON body; the server's own reason ("city not found", "unknown voice") is thrown as the error message. */
export async function put<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: "PUT",
    headers: { "x-api-key": getKey(), "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 401) {
    setKey("");
    window.location.reload();
    throw new Error("401");
  }
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: unknown }) => (typeof j.detail === "string" ? j.detail : ""), () => "");
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

/** DELETE; the server's own reason is thrown as the error message (like `put`). */
export async function del<T>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`, { method: "DELETE", headers: { "x-api-key": getKey() } });
  if (res.status === 401) {
    setKey("");
    window.location.reload();
    throw new Error("401");
  }
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: unknown }) => (typeof j.detail === "string" ? j.detail : ""), () => "");
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

/** POST a JSON body; the server's own reason is thrown as the error message (like `put`). */
export async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: "POST",
    headers: { "x-api-key": getKey(), "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 401) {
    setKey("");
    window.location.reload();
    throw new Error("401");
  }
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: unknown }) => (typeof j.detail === "string" ? j.detail : ""), () => "");
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

/** Like `audioUrl`, and says whether a backup engine spoke instead of the chosen one (for example a Gemini quota). */
export async function audioWithInfo(path: string): Promise<{ url: string; backup: boolean; engine: string }> {
  const res = await fetch(`/api${path}`, { headers: { "x-api-key": getKey() } });
  if (!res.ok) throw new Error(`${res.status}`);
  const engine = res.headers.get("x-voice-engine") ?? "";
  return { url: URL.createObjectURL(await res.blob()), engine, backup: !!engine && engine !== res.headers.get("x-voice-wanted") };
}

/** An audio file behind the key, as a playable object URL (an <audio src> cannot send a header). */
export async function audioUrl(path: string): Promise<string> {
  const res = await fetch(`/api${path}`, { headers: { "x-api-key": getKey() } });
  if (!res.ok) throw new Error(`${res.status}`);
  return URL.createObjectURL(await res.blob());
}

/** Loads `path` now and again every `everyMs` (if given). `reload` fetches immediately. */
export function useApi<T>(path: string, everyMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    api<T>(path)
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, [path]);
  useEffect(() => {
    load();
    if (!everyMs) return;
    const t = setInterval(load, everyMs);
    return () => clearInterval(t);
  }, [load, everyMs]);
  return { data, error, reload: load };
}
