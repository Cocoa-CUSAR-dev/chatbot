import liff from "@line/liff";

const LIFF_ID = import.meta.env.VITE_LIFF_ID;

let initPromise: Promise<void> | null = null;

// Idempotent -- App.tsx's effect can call this more than once across
// StrictMode's double-invoke in dev without double-initializing LIFF.
export function ensureLiffReady(): Promise<void> {
  if (!LIFF_ID) {
    return Promise.reject(
      new Error(
        "VITE_LIFF_ID is not set -- see .env.sample. A real LIFF ID has to be " +
          "issued from the LINE Developers Console for the chatbot's channel " +
          "before this app can run for real (docs-and-plan#176's one manual step).",
      ),
    );
  }
  if (!initPromise) {
    initPromise = liff.init({ liffId: LIFF_ID }).then(() => {
      if (!liff.isLoggedIn()) {
        // Redirects to LINE's login, then back to this same URL -- nothing
        // after this line runs on this page load.
        liff.login();
      }
    });
  }
  return initPromise;
}

// Every API call in api.ts needs this -- getIDToken() throws if init/login
// hasn't completed yet, which ensureLiffReady's caller (App.tsx) is
// responsible for awaiting first.
export function getAuthHeader(): { Authorization: string } {
  const idToken = liff.getIDToken();
  if (!idToken) {
    throw new Error("No LIFF ID token -- ensureLiffReady() must resolve before this is called");
  }
  return { Authorization: `Bearer ${idToken}` };
}

export function closeLiffWindow(): void {
  if (liff.isInClient()) {
    liff.closeWindow();
  }
  // Outside the LINE app (e.g. testing in a plain desktop browser),
  // isInClient() is false and there's no window LIFF can close -- left as
  // a no-op rather than throwing, so local testing doesn't need the real
  // LINE app just to click through the happy path.
}
