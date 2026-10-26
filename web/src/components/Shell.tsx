"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { api, getKey, setKey } from "@/lib/api";
import { setCurrency } from "@/lib/format";

const PANELS = [
  ["/", "Overview"],
  ["/transactions", "Transactions"],
  ["/debates", "Decisions"],
  ["/health", "Health"],
  ["/security", "Security"],
  ["/logs", "Conversations"],
  ["/about", "About me"],
  ["/skills", "Skills"],
  ["/routines", "Routines"],
  ["/diary", "Diary"],
  ["/setup", "Get started"],
  ["/models", "AI Models"],
  ["/news", "News and CVEs"],
  ["/storage", "Storage"],
  ["/settings", "Settings"],
  ["/update", "Updates"],
] as const;

/** The login screen until a key is accepted, then the navigation and the page. */
export default function Shell({ children }: { children: ReactNode }) {
  const path = usePathname();
  const [key, setKeyState] = useState<string | null>(null); // null until the browser has been asked
  const [typed, setTyped] = useState("");
  const [msg, setMsg] = useState("");
  const [ready, setReady] = useState(false); // the home currency is known, so money renders right the first time

  useEffect(() => setKeyState(getKey()), []);
  useEffect(() => {
    if (!key) return;
    api<{ prefs: { currency?: string } }>("/prefs", key)
      .then((p) => setCurrency(p.prefs.currency ?? "IDR"))
      .catch(() => undefined) // an older server or a hiccup: rupiah, as before
      .finally(() => setReady(true));
  }, [key]);

  async function login(e: FormEvent) {
    e.preventDefault();
    setMsg("Checking...");
    try {
      await api("/dashboard/overview", typed.trim());
      setKey(typed.trim());
      setKeyState(typed.trim());
      setMsg("");
    } catch {
      setMsg("Wrong key or the server is not responding.");
    }
  }

  if (key === null) return null;
  if (!key) {
    return (
      <main className="login">
        <h1>Mission Control</h1>
        <p className="muted">Enter the same API key as in the Android app.</p>
        <form onSubmit={login}>
          <input type="password" autoFocus placeholder="API key" value={typed} onChange={(e) => setTyped(e.target.value)} />
          <button type="submit">Sign in</button>
        </form>
        {msg && <p className="muted">{msg}</p>}
      </main>
    );
  }
  return (
    <>
      <nav>
        <strong>Mission Control</strong>
        {PANELS.map(([href, label]) => (
          <Link key={href} href={href} className={path === href ? "on" : ""}>
            {label}
          </Link>
        ))}
        <button
          className="link"
          onClick={() => {
            setKey("");
            setKeyState("");
          }}
        >
          Sign out
        </button>
      </nav>
      <main>{ready ? children : null}</main>
    </>
  );
}
