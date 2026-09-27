const API_BASE_URL = "http://127.0.0.1:8000";
export async function apiGet(path) { const r = await fetch(`${API_BASE_URL}${path}`); if (!r.ok) throw new Error("API request failed"); return r.json(); }
