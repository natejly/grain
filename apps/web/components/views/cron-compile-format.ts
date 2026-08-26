/**
 * The "Next: …" line under a compiled schedule.
 *
 * The compiler answers with UTC instants, but nobody schedules work in UTC in
 * their head — a person who wrote "every monday at 9am" wants to see mondays
 * and 9am, which means rendering each instant in the schedule's own IANA zone.
 * A zone the runtime does not recognise makes `toLocaleString` throw, and the
 * viewer's local wall time is a worse answer than the schedule's but a far
 * better one than a crash, so the fallback drops the zone rather than the line.
 */
export function formatNextFires(isoTimes: string[], timeZone: string): string {
  return isoTimes
    .map((iso) => {
      const at = new Date(iso);
      try {
        return at.toLocaleString(undefined, { timeZone });
      } catch {
        return at.toLocaleString();
      }
    })
    .join(" · ");
}
