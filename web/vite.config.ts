import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
	plugins: [react()],
	server: {
		// npm run dev proxies the event feed to the Pi
		proxy: { "/events": "http://192.168.1.11:8765" },
	},
});
