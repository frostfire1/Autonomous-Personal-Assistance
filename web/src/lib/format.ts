const plain = new Intl.NumberFormat("en-US");

// The ledger's one home currency (rupiah by default). Shell loads the saved choice after sign-in.
let currency = "IDR";
export const setCurrency = (code: string): void => {
  currency = code || "IDR";
};

/** Money in the home currency: "Rp 58,300" for rupiah, otherwise the browser's own currency format ("$12.50"). */
export const rupiah = (n: number | null | undefined): string => {
  if (n == null) return "-";
  if (currency === "IDR") return `Rp ${plain.format(n)}`;
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(n);
  } catch {
    return `${currency} ${plain.format(n)}`;
  }
};

export function when(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString("en-US", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function hm(minutes: number | null | undefined): string {
  if (minutes == null) return "-";
  return `${Math.floor(minutes / 60)} h ${minutes % 60} m`;
}
