import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "SwoopDZ | Live Deal Sniper",
  description: "High-speed flipping tool for the Algerian market.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-[#0f1115] text-slate-100 selection:bg-emerald-500/30">
        <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
          {children}
        </main>
      </body>
    </html>
  );
}
