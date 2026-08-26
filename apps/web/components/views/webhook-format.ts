import type {
  ApiTokenRow,
  WebhookDelivery,
  WebhookEvent,
} from "@workspace/api-client";

/**
 * The pure half of the API & Webhooks view: token states, delivery chips and
 * event labels. React-free so `tests/webhook-format.test.ts` can pin the
 * semantics without a DOM.
 */

/** Every event an endpoint may subscribe to, in the order the form offers them. */
export const WEBHOOK_EVENTS: { event: WebhookEvent; label: string }[] = [
  { event: "run.completed", label: "Run completed" },
  { event: "workflow_run.completed", label: "Workflow run finished" },
  { event: "approval.requested", label: "Approval requested" },
  { event: "monitor.tripped", label: "Monitor tripped" },
];

const EVENT_LABELS = new Map(
  WEBHOOK_EVENTS.map((entry) => [entry.event, entry.label] as const),
);

/** The short label an event wears on a chip; the raw name for a stranger. */
export function eventLabel(event: string): string {
  return EVENT_LABELS.get(event as WebhookEvent) ?? event;
}

export type TokenState = "active" | "revoked";

export function tokenState(
  token: Pick<ApiTokenRow, "revoked_at">,
): TokenState {
  return token.revoked_at ? "revoked" : "active";
}

/**
 * What a token row says about its use. "Never used" is worth stating out
 * loud: a stale credential nobody calls with is the one worth revoking.
 */
export function tokenUseLabel(
  token: Pick<ApiTokenRow, "last_used_at">,
): string {
  if (!token.last_used_at) return "Never used";
  return `Last used ${new Date(token.last_used_at).toLocaleDateString()}`;
}

/**
 * The status-pill tone for a delivery chip, mapped onto the CSS classes the
 * shell already has: `ready` is green, `error` is red, "" is the neutral
 * default — pending is not a problem, it is a queue.
 */
export function deliveryTone(
  delivery: Pick<WebhookDelivery, "status">,
): "ready" | "error" | "" {
  if (delivery.status === "sent") return "ready";
  if (delivery.status === "failed") return "error";
  return "";
}

/**
 * What a delivery row reads. A failure names its attempts and error, because
 * "failed" alone is a support ticket with no evidence.
 */
export function deliveryLabel(
  delivery: Pick<WebhookDelivery, "status" | "attempts" | "last_error">,
): string {
  if (delivery.status === "sent") return "Delivered";
  if (delivery.status === "failed") {
    const reason = delivery.last_error ? `: ${delivery.last_error}` : "";
    return `Failed after ${delivery.attempts} ${
      delivery.attempts === 1 ? "attempt" : "attempts"
    }${reason}`;
  }
  return delivery.attempts > 0 ? "Retrying" : "Pending";
}

/** Deliveries for one endpoint, newest first — or all of them, same order. */
export function deliveriesFor(
  deliveries: WebhookDelivery[],
  endpointId?: string,
): WebhookDelivery[] {
  return deliveries
    .filter(
      (delivery) => !endpointId || delivery.endpoint_id === endpointId,
    )
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
}
