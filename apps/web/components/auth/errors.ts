import { describeError } from "../views/shared";

/**
 * Same rule as `describeError`, except these pages stand alone: there is no
 * API-down banner behind them to explain an empty string, so an unreachable API
 * has to say so here.
 */
export function authError(caught: unknown, fallback: string): string {
  return (
    describeError(caught, fallback) ||
    "Cannot reach the API right now. Try again in a moment."
  );
}
