import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Mail Pilot · 把学校邮箱变成一份要做的清单",
  description:
    "Mail Pilot 只读接收 CityU 学校邮箱，自动分清课程、行政、社团与校招信息，每天发回一封摘要。",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="zh-Hans">
      <body>{children}</body>
    </html>
  );
}
