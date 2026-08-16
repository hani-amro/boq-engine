/**
 * The one QueryClient, in a module rather than in `main.tsx`.
 *
 * The store saves the project outside React — a debounced autosave fired from a
 * timer, and a hand-off fired while a route is changing — and the cache entry for
 * that project must move with it. A `useQueryClient()` hook cannot be reached from
 * there, and a second QueryClient would be a second, disagreeing copy of the
 * document: the editor would save one version and re-open another.
 */
import { QueryClient } from '@tanstack/react-query'

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // The API client already raised an Arabic toast for the failure, so a retry
      // storm would be a toast storm. One retry covers a flaky connection; more
      // than that just delays telling the user something is wrong.
      retry: 1,
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
    mutations: {
      retry: 0,
    },
  },
})
