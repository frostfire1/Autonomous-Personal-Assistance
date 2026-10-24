const idr = new Intl.NumberFormat("en-US");

export const rupiah = (n: number | null | undefined): string => (n == null ? "-" : `Rp ${idr.format(n)}`);

export function when(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString("en-US", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function hm(minutes: number | null | undefined): string {
  if (minutes == null) return "-";
  return `${Math.floor(minutes / 60)} h ${minutes % 60} m`;
}
