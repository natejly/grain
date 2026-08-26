import type { Metadata, Viewport } from "next";
import { THEME_INIT_SCRIPT } from "../components/theme-script";
// Global rather than imported by whichever view happens to render maths.
// It lived in views/documents.tsx, so a chat message with $x^2$ rendered
// unstyled for anyone who had not opened Documents first.
import "katex/dist/katex.min.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Grain",
  description: "Cited knowledge, graph projections, dashboards, and published snapshots.",
};

// viewport-fit=cover is what arms env(safe-area-inset-*): without it the
// mobile tab bar's home-indicator padding evaluates to zero on every notched
// phone and the bar's bottom row sits under the pill.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Applies a stored theme choice before the first paint. Without it a
            viewer whose choice opposes their OS preference sees one frame of
            the wrong theme; `suppressHydrationWarning` above is because this
            script mutates <html> before React reaches it. */}
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
