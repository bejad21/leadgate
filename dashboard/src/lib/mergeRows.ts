/**
 * Combine rows that arrived live with rows read from the database. A live row
 * can arrive before the first read finishes, so the read must not overwrite it:
 * live rows come first and win when both have the same id.
 */
export function mergeRows<T extends { id: number }>(live: T[], fetched: T[]): T[] {
  const seen = new Set(live.map((row) => row.id))
  return [...live, ...fetched.filter((row) => !seen.has(row.id))]
}
