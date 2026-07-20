import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Cornell Tech MakerLAB — Bambu Farm",
  description:
    "Live printer status, camera snapshots, and student print submission for the Cornell Tech MakerLAB Bambu farm.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
