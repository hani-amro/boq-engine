/**
 * The quote route (`/p/:id/quote`) points at this module in `src/App.tsx`.
 *
 * The screen itself lives in `./Quote` — this file is only the name the router
 * already knows, kept so the route wiring in the shell stays untouched. The
 * blocking rule the shell placeholder carried is implemented there, and harder:
 * `blocked_reason_ar` (or a placeholder price book with no reason attached) leads
 * with a red banner, disables print / download / open-in-tab, and stripes the
 * document preview, so a blocked quote can never be mistaken for a sendable one.
 */
export { default } from './Quote'
