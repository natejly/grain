"use client";

import type { LucideIcon } from "lucide-react";

/**
 * The one shared empty-state: icon + one line + a single primary action.
 *
 * Renders the existing `.feature-empty` chrome (the graph page's pattern) so
 * an empty view reads like every other empty view, and adds the two things the
 * weakest empties were missing: a sentence that says what would fill the page,
 * and a button that goes where that happens. The action pair is optional —
 * a view with nowhere to send the user still gets the consistent shell.
 */
export function EmptyState({
  icon: Icon,
  title,
  line,
  actionLabel,
  onAction,
}: {
  icon: LucideIcon;
  title: string;
  line: string;
  actionLabel?: string;
  onAction?: () => void;
}) {
  return (
    <div className="feature-empty">
      <Icon size={20} aria-hidden="true" />
      <strong>{title}</strong>
      <span>{line}</span>
      {actionLabel && onAction && (
        <button type="button" className="primary-button empty-action" onClick={onAction}>
          {actionLabel}
        </button>
      )}
    </div>
  );
}
