import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The floating dev badge sits exactly on top of the ⌘K button in the sidebar's
  // bottom-left corner. It never ships, but it makes the corner unreviewable while
  // developing, which is the one time anybody is looking closely at it.
  devIndicators: false,
};

export default nextConfig;
