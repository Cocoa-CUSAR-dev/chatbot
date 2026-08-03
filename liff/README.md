# LIFF app

Placeholder. Per [ADR 0003](../README.md#related-adrs), the LIFF frontend (pairing-code display, any rich in-chat forms) is a **separate, lightweight Vite + React app**, not folded into the FastAPI backend or the existing Next.js `web-app`.

Not yet scaffolded — nothing else in this repo depends on it existing yet (the `/identity/link` endpoint in `src/identity` can be called directly for now). Scaffold with `npm create vite@latest . -- --template react-ts` when work on it actually starts.
