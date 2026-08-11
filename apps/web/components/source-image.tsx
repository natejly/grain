"use client";

import type { ToolArtifact } from "@workspace/api-client";
import { useEffect, useState } from "react";
import { api } from "./api";

/**
 * A picture the workspace stored, shown.
 *
 * The point of this component is that the obvious version does not work. The
 * bytes live behind an authenticated route on the API origin, which is a
 * different *site* from the web app, so `<img src={api}/sources/x/content>`
 * fails twice over: the request carries no `X-Workspace-Id` (and the server
 * then answers for whichever workspace the user joined first), and it is a
 * third-party subresource whose cookie Safari already refuses and Chrome is
 * withdrawing. Either way the user gets a broken image and no explanation.
 *
 * So the bytes are fetched the way every other call in this app is made —
 * credentialed, workspace-scoped, through the API client — and handed to the
 * `<img>` as an object URL, which is same-origin by construction. The cost is
 * one revoke on unmount, which is why this is a component and not a helper.
 */
export function SourceImage({
  sourceId,
  alt,
  width,
  height,
}: {
  sourceId: string;
  /** Real alt text. Never "" and never the filename. */
  alt: string;
  width?: number | null;
  height?: number | null;
}) {
  const [href, setHref] = useState("");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let url = "";
    let live = true;
    setFailed(false);
    setHref("");
    void api
      .sourceContent(sourceId)
      .then((blob) => {
        // Created before the liveness check so a blob fetched for a component
        // that unmounted mid-flight is still revoked rather than leaked.
        url = URL.createObjectURL(blob);
        if (live) setHref(url);
        else URL.revokeObjectURL(url);
      })
      .catch(() => {
        if (live) setFailed(true);
      });
    return () => {
      live = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [sourceId]);

  if (failed) {
    // Said out loud. The bug this whole component exists to fix was a figure
    // that failed to render *silently*, which is indistinguishable from a run
    // that drew nothing — and led to a year of nobody noticing.
    return (
      <p className="artifact-missing" role="status">
        This image could not be loaded.
      </p>
    );
  }
  if (!href) {
    // The box is reserved at the image's own size when the server read one off
    // the file header, so the transcript does not jump when it arrives.
    return (
      <div
        className="artifact-pending"
        style={width && height ? { aspectRatio: `${width} / ${height}` } : undefined}
        aria-hidden="true"
      />
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      className="artifact-image"
      src={href}
      alt={alt}
      width={width ?? undefined}
      height={height ?? undefined}
    />
  );
}

/**
 * The images among a tool call's artifacts, or nothing.
 *
 * Shared by the sandbox console and the chat transcript because "a chart the
 * agent drew" is one idea that happens to have two homes — the panel where you
 * ran the code, and the conversation where you asked for it.
 */
export function ArtifactImages({
  artifacts,
  label,
}: {
  artifacts: ToolArtifact[];
  /** What produced these, for the alt text: "this execution", "run_python". */
  label: string;
}) {
  const images = artifacts.filter((artifact) => artifact.mime.startsWith("image/"));
  if (images.length === 0) return null;
  return (
    <div className="artifact-images">
      {images.map((artifact, index) => (
        <SourceImage
          key={artifact.id}
          sourceId={artifact.id}
          // The only thing that can honestly be said about a figure whose
          // contents nothing here has read. Numbered because "Figure 2" is how
          // someone refers to it in the next message.
          alt={`Figure ${index + 1} of ${images.length} produced by ${label}`}
          width={artifact.width}
          height={artifact.height}
        />
      ))}
    </div>
  );
}
