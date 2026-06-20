import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Soy Mission Control",
  description: "Mission orchestration dashboard for Soy backend",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-zinc-950 text-zinc-100 antialiased">
        <header className="border-b border-zinc-800 px-6 py-3">
          <nav className="mx-auto flex max-w-6xl items-center justify-between">
            <a href="/" className="text-lg font-bold tracking-tight">
              Soy
            </a>
            <a
              href="/missions/new"
              className="rounded-md bg-zinc-100 px-3 py-1.5 text-sm font-medium text-zinc-900 hover:bg-zinc-200 transition-colors"
            >
              New Mission
            </a>
          </nav>
        </header>
        <main className="mx-auto max-w-6xl px-6 py-8">{children}</main>
      </body>
    </html>
  );
}
