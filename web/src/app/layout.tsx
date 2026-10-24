import type { ReactNode } from "react";
import Shell from "@/components/Shell";
import "./globals.css";

export const metadata = { title: "Mission Control", description: "Personal finance, health and security" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <head>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
      </head>
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
