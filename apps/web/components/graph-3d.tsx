"use client";

import type { GraphEdge, GraphEntity } from "@workspace/api-client";
import { useEffect, useRef, useState } from "react";
import type { Theme } from "./theme";
import { useTheme } from "./theme";

/**
 * An Obsidian-style force-directed knowledge graph, in 3D.
 *
 * three.js is loaded dynamically so only this view pays for it — the same
 * treatment esbuild-wasm and the TeX engine get. Everything is drawn from two
 * draw calls' worth of geometry (one InstancedMesh for nodes, a few
 * LineSegments for edges) so a few thousand entities stay interactive, and
 * clutter is managed visually rather than by dropping data: edge width tracks
 * weight, typed relations are solid while co-occurrence is dashed and dim, and
 * labels appear only for the nodes that earn them.
 */

export type Graph3DProps = {
  entities: GraphEntity[];
  edges: GraphEdge[];
  /** Clicking a node selects it; the panel below the canvas reacts. */
  onSelect?: (entity: GraphEntity | null) => void;
  /** The row picked in the list beside the canvas; lit like a hover. */
  selectedId?: string | null;
  className?: string;
};

type Simulated = { id: string; radius: number; x: number; y: number; z: number };

/** How many labels can exist at once. Each one costs a canvas texture. */
const MAX_LABELS = 40;
/**
 * On-screen height of a label, in CSS pixels — labels are sized in screen space,
 * not in the world, so this is a real type size and not a scale factor. The
 * texture is drawn at 28px into a 40px box, so the glyphs land near 14px.
 */
const LABEL_PIXELS = 20;
/** Breathing room around a label's box when the declutter pass tests overlap. */
const LABEL_GUTTER = 4;
const NODE_SEGMENTS = 12;

/**
 * three.js takes literal colours, not CSS variables, so this is the one place
 * in the app that has to duplicate the palette. It is a full palette per theme
 * rather than a light tweak: the fog colour has to match the page or the graph
 * sits in a rectangle of the wrong ground, and hues that read on #0b0d10 wash
 * out on cream. Values track the tokens in globals.css.
 *
 * Entity hues are distinct at a glance without being a rainbow, and each one
 * clears 3:1 against its theme's page background — nodes are the content here,
 * so they are graphical objects under WCAG 1.4.11.
 */
type GraphPalette = {
  fog: number;
  types: Record<string, number>;
  fallback: number;
  typedEdge: number;
  looseEdge: number;
  label: string;
  ambient: number;
  ambientIntensity: number;
  keyIntensity: number;
};

const PALETTES: Record<Theme, GraphPalette> = {
  dark: {
    fog: 0x0b0d10,
    types: {
      organization: 0xe2b86b,
      project: 0x7c9cff,
      named_entity: 0x64c78d,
      concept: 0x929ba8,
    },
    // See the light palette's note: the fallback is deliberately not any
    // type's colour, so "unknown" stays visibly unknown.
    fallback: 0x5c6672,
    typedEdge: 0x7c9cff,
    looseEdge: 0x6b7684,
    label: "#e7eaf0",
    ambient: 0xffffff,
    ambientIntensity: 0.75,
    keyIntensity: 0.8,
  },
  light: {
    fog: 0xfaf7f0,
    types: {
      organization: 0xa87c57,
      // Blue, matching the dark theme's hue assignment — the old #2c7454 was
      // a green a swatch-width away from named_entity's #37784b, so the
      // legend showed two indistinguishable dots (and collided with the
      // typed-edge accent line besides).
      project: 0x3b5fc0,
      named_entity: 0x37784b,
      concept: 0x6b6357,
    },
    // Its own gray, NOT concept's: a node of an unknown type must not
    // masquerade as a concept in either theme.
    fallback: 0x968b7b,
    typedEdge: 0x2c7454,
    looseEdge: 0x83887c,
    label: "#2e2a24",
    ambient: 0xffffff,
    // Lambert nodes on a light ground need less fill and a firmer key, or
    // every sphere flattens into a flat disc of its own colour.
    ambientIntensity: 0.55,
    keyIntensity: 0.95,
  },
};

function colorFor(palette: GraphPalette, entity: GraphEntity): number {
  return palette.types[entity.entity_type] ?? palette.fallback;
}

/**
 * The entity-type half of the on-canvas legend, derived from the same palette
 * the spheres are coloured from so the swatch and the node it explains can
 * never disagree. Labels are for people, not the schema: `named_entity` reads
 * as "entity".
 */
export function entityLegend(
  theme: Theme,
  /** Include the fallback swatch — the caller says whether any node wears it,
   *  because a legend row for a colour nothing uses is noise. */
  includeFallback = false,
): { type: string; label: string; color: string }[] {
  const hexOf = (hex: number) => `#${hex.toString(16).padStart(6, "0")}`;
  const rows = Object.entries(PALETTES[theme].types).map(([type, hex]) => ({
    type,
    label: type === "named_entity" ? "entity" : type,
    color: hexOf(hex),
  }));
  if (includeFallback) {
    rows.push({ type: "fallback", label: "other", color: hexOf(PALETTES[theme].fallback) });
  }
  return rows;
}

/**
 * Everything the scene is built out of, as one comparable string.
 *
 * Rebuilding means a fresh WebGL context, a fresh simulation and a camera that
 * jumps back to its framing shot, so it has to happen when the *graph* changes
 * — not when the page around the canvas re-renders. Callers necessarily pass
 * array literals (`graph?.edges.filter(...)`), so prop identity is new on every
 * render, and this view re-renders plenty while it is open: the 400ms poll
 * behind a rebuild, the refresh after a chat turn, the click that selects a
 * node. Keyed on identity, each of those threw the layout away and started the
 * graph over from noise — and leaked a context doing it.
 *
 * NUL-joined rather than on a printable character: entity names come from
 * extraction and a "|" or a ":" in one is ordinary, where a NUL is not.
 */
export function graphSignature(entities: GraphEntity[], edges: GraphEdge[]): string {
  const parts: string[] = [];
  for (const entity of entities) {
    // Every field the scene reads: id and type colour it, mentions size it,
    // name is baked into a label texture.
    parts.push(entity.id, entity.entity_type, String(entity.mention_count), entity.name);
  }
  parts.push("\u0000edges");
  for (const edge of edges) {
    parts.push(edge.from_entity_id, edge.to_entity_id, edge.relation);
  }
  return parts.join("\u0000");
}

/** One label, already projected to pixels by the caller. */
export type LabelPlacement = {
  /** Bottom-centre of the label, in CSS pixels from the top-left of the canvas. */
  anchorX: number;
  anchorY: number;
  /** Width / height of the label's texture, which sets its pixel width. */
  aspect: number;
  /** Behind the camera, where a billboarded sprite would otherwise reappear. */
  behind: boolean;
};

/**
 * Hand out the screen to labels, in the order given — highest priority first.
 *
 * Names are positioned by the graph, which knows nothing about how long they
 * are, so in any cluster they overlap into an unreadable heap — and the heap is
 * worst exactly where the graph is most interesting. So each label claims its
 * pixel box if it can, and one whose box overlaps a box already claimed steps
 * aside until the camera moves it clear. Callers pass the labels most-mentioned
 * first, which is what makes the hub keep its name and the passing mention
 * yield.
 *
 * Screen space, and therefore per frame: it depends on where the camera is.
 * Cheap enough to be — MAX_LABELS is 40, so the worst case is ~800 rectangle
 * tests.
 */
export function placeLabels(
  placements: LabelPlacement[],
  viewWidth: number,
  viewHeight: number,
): boolean[] {
  const claimed: { left: number; right: number; top: number; bottom: number }[] = [];
  return placements.map((placement) => {
    if (placement.behind) return false;
    const halfWidth = (LABEL_PIXELS * placement.aspect) / 2 + LABEL_GUTTER;
    // The box grows upward from the anchor, because the anchor is the label's
    // bottom edge rather than its middle.
    const box = {
      left: placement.anchorX - halfWidth,
      right: placement.anchorX + halfWidth,
      top: placement.anchorY - LABEL_PIXELS - LABEL_GUTTER,
      bottom: placement.anchorY + LABEL_GUTTER,
    };
    // A label the viewport cuts in half reads as a truncated name, so one that
    // does not fit inside steps aside and comes back when the camera brings it
    // in. Unless it could never fit at this size — then clipped beats a node
    // that can never show its name.
    const fits = box.right - box.left <= viewWidth && box.bottom - box.top <= viewHeight;
    if (fits && (box.left < 0 || box.right > viewWidth || box.top < 0 || box.bottom > viewHeight)) {
      return false;
    }
    const collides = claimed.some(
      (other) =>
        box.left < other.right &&
        box.right > other.left &&
        box.top < other.bottom &&
        box.bottom > other.top,
    );
    if (collides) return false;
    claimed.push(box);
    return true;
  });
}

/** Node radius grows with mentions, but sublinearly — a hub must not eat the view. */
function radiusFor(entity: GraphEntity): number {
  return 4.5 + Math.sqrt(Math.max(0, entity.mention_count)) * 2.2;
}

export function Graph3D({ entities, edges, onSelect, selectedId = null, className }: Graph3DProps) {
  const mountRef = useRef<HTMLDivElement>(null);
  const [failure, setFailure] = useState("");
  // The callback is read from a ref so a caller passing an inline arrow does not
  // tear down and rebuild the whole scene on every render.
  const selectRef = useRef(onSelect);
  selectRef.current = onSelect;
  // Same ref treatment for the list-side selection: keying the build effect on
  // it would tear down and rebuild the whole scene on every row click, so the
  // scene instead exposes its highlight pass and a selection change replays it.
  const selectedRef = useRef<string | null>(selectedId);
  selectedRef.current = selectedId;
  const highlightRef = useRef<(() => void) | null>(null);
  useEffect(() => {
    highlightRef.current?.();
  }, [selectedId]);
  // The data the scene is built from is read through refs for the same reason:
  // the build effect keys on `signature` (content), and the refs are what let
  // it reach the arrays that signature describes without keying on identity.
  const entitiesRef = useRef(entities);
  entitiesRef.current = entities;
  const edgesRef = useRef(edges);
  edgesRef.current = edges;
  const signature = graphSignature(entities, edges);
  // Not a ref: a theme change has to rebuild the scene, because fog, lighting
  // and every instanced colour are baked in at build time.
  const theme = useTheme();

  useEffect(() => {
    const mount = mountRef.current;
    // Pinned for the lifetime of this scene: `signature` says these are the
    // entities and edges it was built for.
    const entities = entitiesRef.current;
    const edges = edgesRef.current;
    if (!mount || entities.length === 0) return;
    const palette = PALETTES[theme];

    let disposed = false;
    let cleanup: (() => void) | null = null;

    // Everything below is dynamically imported, so three.js never lands in the
    // main bundle. The async gap is why `disposed` is checked before building.
    void (async () => {
      let THREE: typeof import("three");
      let OrbitControls: typeof import("three/examples/jsm/controls/OrbitControls.js")["OrbitControls"];
      let forceSimulation: typeof import("d3-force-3d")["forceSimulation"];
      let forceLink: typeof import("d3-force-3d")["forceLink"];
      let forceManyBody: typeof import("d3-force-3d")["forceManyBody"];
      let forceCenter: typeof import("d3-force-3d")["forceCenter"];
      let forceCollide: typeof import("d3-force-3d")["forceCollide"];
      try {
        [
          THREE,
          { OrbitControls },
          { forceSimulation, forceLink, forceManyBody, forceCenter, forceCollide },
        ] = await Promise.all([
            import("three"),
            import("three/examples/jsm/controls/OrbitControls.js"),
            import("d3-force-3d"),
          ]);
      } catch {
        if (!disposed) setFailure("The 3D renderer could not be loaded.");
        return;
      }
      if (disposed) return;

      const width = mount.clientWidth || 800;
      const height = mount.clientHeight || 520;
      // The live viewport, in CSS pixels. Labels are sized and laid out against
      // it, so it is tracked rather than captured — the ResizeObserver keeps it
      // current.
      let viewWidth = width;
      let viewHeight = height;

      let renderer: import("three").WebGLRenderer;
      try {
        renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
      } catch {
        // Software rendering, a blocked context, or a headless browser.
        if (!disposed) setFailure("This browser has no available WebGL context.");
        return;
      }
      renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
      renderer.setSize(width, height);
      mount.appendChild(renderer.domElement);
      renderer.domElement.style.display = "block";
      renderer.domElement.style.touchAction = "none";

      const scene = new THREE.Scene();
      // Fog is the depth cue that makes a 3D point cloud readable; without it a
      // far node and a small near node are indistinguishable.
      scene.fog = new THREE.Fog(palette.fog, 180, 620);

      const camera = new THREE.PerspectiveCamera(55, width / height, 0.1, 2000);
      camera.position.set(0, 0, 260);

      const controls = new OrbitControls(camera, renderer.domElement);
      controls.enableDamping = true;
      controls.dampingFactor = 0.08;
      controls.rotateSpeed = 0.6;
      controls.minDistance = 40;
      controls.maxDistance = 900;

      scene.add(new THREE.AmbientLight(palette.ambient, palette.ambientIntensity));
      const key = new THREE.DirectionalLight(0xffffff, palette.keyIntensity);
      key.position.set(1, 1, 1);
      scene.add(key);

      // ---- simulation -------------------------------------------------
      const nodes: Simulated[] = entities.map((entity) => ({
        id: entity.id,
        // The drawn radius, carried on the node so the collision force and the
        // renderer cannot drift apart.
        radius: radiusFor(entity),
        x: (Math.random() - 0.5) * 120,
        y: (Math.random() - 0.5) * 120,
        z: (Math.random() - 0.5) * 120,
      }));
      const index = new Map(entities.map((entity, position) => [entity.id, position]));
      // Only edges whose endpoints are both present can be simulated; the API
      // may return an edge to an entity trimmed by the limit.
      const links = edges
        .filter(
          (edge) => index.has(edge.from_entity_id) && index.has(edge.to_entity_id),
        )
        .map((edge) => ({
          source: index.get(edge.from_entity_id)!,
          target: index.get(edge.to_entity_id)!,
          // forceLink rewrites source/target in place; these survive it.
          fromIndex: index.get(edge.from_entity_id)!,
          toIndex: index.get(edge.to_entity_id)!,
          edge,
        }));

      const simulation = forceSimulation(nodes, 3)
        .force(
          "link",
          forceLink(links)
            // A hub is a big sphere, so a fixed link distance buries its
            // neighbours inside it. Measure the gap between surfaces instead.
            .distance(
              (link: { fromIndex: number; toIndex: number }) =>
                30 + nodes[link.fromIndex].radius + nodes[link.toIndex].radius,
            )
            .id((_node: unknown, position: number) => position)
            .strength(0.25),
        )
        .force("charge", forceManyBody().strength(-150))
        // Repulsion alone is a tug-of-war with the links and loses: at a few
        // dozen entities the layout packed spheres into each other and the
        // graph read as one blob with names on top. Collision is the part that
        // knows how big a node is actually drawn, so nodes stop overlapping
        // without the whole graph having to fly apart to achieve it.
        .force(
          "collide",
          forceCollide((node: Simulated) => node.radius + 6).strength(0.85),
        )
        .force("center", forceCenter(0, 0, 0))
        .stop();

      // Settle off-screen, all the way. The camera distance below is computed
      // from the layout, so it has to be computed from the layout the user will
      // actually look at — framing a half-settled one and then letting it grow
      // for another ninety frames is how the graph ended up small and adrift in
      // its box. The cap is a guard for a graph that will not converge.
      for (let tick = 0; tick < 400 && simulation.alpha() > simulation.alphaMin(); tick += 1) {
        simulation.tick();
      }

      // Frame whatever the simulation produced. A fixed camera distance turns a
      // five-node graph into specks and buries a large one.
      let extent = 0;
      for (const node of nodes) {
        extent = Math.max(extent, Math.hypot(node.x, node.y, node.z));
      }
      const nodeMargin = Math.max(...entities.map(radiusFor), 6);
      const span = Math.max(extent + nodeMargin * 2, 40);
      // 1.15, not 1.6: `span` already carries a node's worth of margin on each
      // side, and the labels that used to need the rest of the slack are
      // screen-space now. The old figure left the graph sitting in the middle
      // of a large empty panel.
      const distance = (span / Math.tan((camera.fov * Math.PI) / 360)) * 1.15;
      camera.position.set(0, 0, distance);
      camera.updateProjectionMatrix();
      controls.maxDistance = distance * 4;
      scene.fog = new THREE.Fog(palette.fog, distance * 0.55, distance * 2.6);

      // ---- nodes ------------------------------------------------------
      const geometry = new THREE.SphereGeometry(1, NODE_SEGMENTS, NODE_SEGMENTS);
      const material = new THREE.MeshLambertMaterial();
      const mesh = new THREE.InstancedMesh(geometry, material, entities.length);
      mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      const baseColors = new Float32Array(entities.length * 3);
      const color = new THREE.Color();
      entities.forEach((entity, position) => {
        color.setHex(colorFor(palette, entity));
        baseColors[position * 3] = color.r;
        baseColors[position * 3 + 1] = color.g;
        baseColors[position * 3 + 2] = color.b;
      });
      mesh.instanceColor = new THREE.InstancedBufferAttribute(
        Float32Array.from(baseColors),
        3,
      );
      scene.add(mesh);

      // ---- edges ------------------------------------------------------
      // Two passes rather than one: WebGL ignores line width, so "thicker" is
      // expressed as brightness and opacity, and the typed/co-occurrence split
      // is what actually carries meaning.
      const typedLinks = links.filter((link) => link.edge.relation !== "co_occurs");
      const looseLinks = links.filter((link) => link.edge.relation === "co_occurs");

      function buildLines(
        subset: typeof links,
        options: { opacity: number; dashed: boolean; hex: number },
      ) {
        const positions = new Float32Array(subset.length * 6);
        const lineGeometry = new THREE.BufferGeometry();
        lineGeometry.setAttribute(
          "position",
          new THREE.BufferAttribute(positions, 3),
        );
        const lineMaterial = options.dashed
          ? new THREE.LineDashedMaterial({
              color: options.hex,
              transparent: true,
              opacity: options.opacity,
              dashSize: 3,
              gapSize: 3,
            })
          : new THREE.LineBasicMaterial({
              color: options.hex,
              transparent: true,
              opacity: options.opacity,
            });
        const lines = new THREE.LineSegments(lineGeometry, lineMaterial);
        // Distances are recomputed each frame for the dashed material; without
        // them a dashed line renders solid.
        lines.frustumCulled = false;
        scene.add(lines);
        return { lines, positions, geometry: lineGeometry, material: lineMaterial, subset };
      }

      const typed = buildLines(typedLinks, {
        opacity: 0.85,
        dashed: false,
        hex: palette.typedEdge,
      });
      const loose = buildLines(looseLinks, {
        opacity: 0.5,
        dashed: true,
        hex: palette.looseEdge,
      });

      // ---- labels -----------------------------------------------------
      // Sprites billboard for free, which is what makes text readable while the
      // camera orbits. Only the most-mentioned entities get one: each label is a
      // canvas texture, and hundreds of them cost more than the graph itself.
      //
      // They are sized in screen space (see `sizeAttenuation` below), so the
      // sort order is doing a second job: it is the priority the declutter pass
      // hands out the screen to, most-mentioned first.
      const labelled = [...entities]
        .sort((a, b) => b.mention_count - a.mention_count)
        .slice(0, MAX_LABELS);
      const sprites: import("three").Sprite[] = [];
      const labelTextures: import("three").Texture[] = [];
      for (const entity of labelled) {
        const canvas = document.createElement("canvas");
        const context = canvas.getContext("2d");
        if (!context) continue;
        const text = entity.name.length > 28 ? `${entity.name.slice(0, 27)}…` : entity.name;
        context.font = "600 28px ui-sans-serif, system-ui, sans-serif";
        canvas.width = Math.ceil(context.measureText(text).width) + 16;
        canvas.height = 40;
        // Resizing the canvas resets the context, so the font is set again.
        context.font = "600 28px ui-sans-serif, system-ui, sans-serif";
        context.fillStyle = palette.label;
        context.textBaseline = "middle";
        context.fillText(text, 8, 21);
        const texture = new THREE.CanvasTexture(canvas);
        texture.colorSpace = THREE.SRGBColorSpace;
        labelTextures.push(texture);
        const sprite = new THREE.Sprite(
          new THREE.SpriteMaterial({
            map: texture,
            transparent: true,
            depthWrite: false,
            // The fix for a wall of names in wildly different sizes. With
            // attenuation on, a label was scaled by perspective like the sphere
            // it names: the ones at the front of the cloud grew until they
            // covered a third of the graph, the ones at the back shrank past
            // reading. Off, three cancels the perspective divide and every
            // label is the same height on screen wherever its node sits.
            sizeAttenuation: false,
            // A label is annotation, not scenery: depth-tested, a sphere that
            // happens to sit nearer the camera sliced the name off mid-word
            // ("Kwame Boa"), which reads as a truncation bug rather than as
            // occlusion. Labels draw over the graph instead, and the declutter
            // pass below is what keeps them from drawing over each other.
            depthTest: false,
          }),
        );
        sprite.renderOrder = 2;
        // Anchor the label by its bottom edge, not its middle. The sprite's
        // position is a point just above the sphere; with the default centre
        // the lower half of the name hung back over the node it names, and how
        // badly depended on depth. Anchored, it clears the sphere at any
        // distance.
        sprite.center.set(0.5, 0);
        sprite.userData.entityId = entity.id;
        // The label's aspect, so a resize can rescale it from one number.
        sprite.userData.aspect = canvas.width / canvas.height;
        sprites.push(sprite);
        scene.add(sprite);
      }

      /**
       * Size every label to LABEL_PIXELS tall, whatever the viewport is.
       *
       * With attenuation off a sprite's scale is in world units *per unit of
       * depth*, and the viewport spans `2 * tan(fov / 2)` of those at any
       * depth — so a label's share of the viewport height is just
       * `scale.y / (2 * tan(fov / 2))`, and this inverts that. Height drives
       * both axes because the projection divides x by the aspect ratio, which
       * is exactly what makes equal world offsets equal pixel offsets.
       */
      function applyLabelScale() {
        const unit =
          (LABEL_PIXELS / Math.max(viewHeight, 1)) * 2 * Math.tan((camera.fov * Math.PI) / 360);
        for (const sprite of sprites) {
          sprite.scale.set(unit * (sprite.userData.aspect as number), unit, 1);
        }
      }
      applyLabelScale();

      // ---- interaction ------------------------------------------------
      const raycaster = new THREE.Raycaster();
      const pointer = new THREE.Vector2();
      const neighbours = new Map<string, Set<string>>();
      for (const link of links) {
        const a = entities[link.fromIndex]?.id ?? "";
        const b = entities[link.toIndex]?.id ?? "";
        if (!a || !b) continue;
        if (!neighbours.has(a)) neighbours.set(a, new Set());
        if (!neighbours.has(b)) neighbours.set(b, new Set());
        neighbours.get(a)!.add(b);
        neighbours.get(b)!.add(a);
      }

      let hoveredId: string | null = null;
      let pointerInside = false;

      function applyHighlight() {
        const attribute = mesh.instanceColor;
        if (!attribute) return;
        // Hover wins while the pointer is on a node; the list selection holds
        // the highlight the rest of the time.
        const activeId = hoveredId ?? selectedRef.current;
        const near = activeId
          ? new Set([activeId, ...(neighbours.get(activeId) ?? [])])
          : null;
        for (let position = 0; position < entities.length; position += 1) {
          const dim = near !== null && !near.has(entities[position].id);
          const scale = dim ? 0.22 : 1;
          attribute.array[position * 3] = baseColors[position * 3] * scale;
          attribute.array[position * 3 + 1] = baseColors[position * 3 + 1] * scale;
          attribute.array[position * 3 + 2] = baseColors[position * 3 + 2] * scale;
        }
        attribute.needsUpdate = true;
        for (const sprite of sprites) {
          const id = sprite.userData.entityId as string;
          sprite.material.opacity = near === null || near.has(id) ? 1 : 0.12;
        }
        typed.material.opacity = activeId ? 0.35 : 0.9;
        loose.material.opacity = activeId ? 0.14 : 0.5;
      }
      highlightRef.current = applyHighlight;

      function pick(event: PointerEvent): number | null {
        const rect = renderer.domElement.getBoundingClientRect();
        pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
        pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
        raycaster.setFromCamera(pointer, camera);
        const hits = raycaster.intersectObject(mesh);
        const first = hits[0];
        return first && first.instanceId !== undefined ? first.instanceId : null;
      }

      function onPointerMove(event: PointerEvent) {
        pointerInside = true;
        const instance = pick(event);
        const id = instance === null ? null : entities[instance].id;
        if (id !== hoveredId) {
          hoveredId = id;
          applyHighlight();
          renderer.domElement.style.cursor = id ? "pointer" : "grab";
        }
      }

      function onPointerLeave() {
        pointerInside = false;
        if (hoveredId !== null) {
          hoveredId = null;
          applyHighlight();
        }
      }

      function onClick(event: PointerEvent) {
        const instance = pick(event);
        selectRef.current?.(instance === null ? null : entities[instance]);
      }

      renderer.domElement.addEventListener("pointermove", onPointerMove);
      renderer.domElement.addEventListener("pointerleave", onPointerLeave);
      renderer.domElement.addEventListener("click", onClick as EventListener);

      // ---- frame loop -------------------------------------------------
      const dummy = new THREE.Object3D();
      let frame = 0;
      let settling = 90;
      // Reused by the per-frame label pass.
      const projected = new THREE.Vector3();
      const placements: LabelPlacement[] = [];

      function syncGeometry() {
        for (let position = 0; position < entities.length; position += 1) {
          const node = nodes[position];
          dummy.position.set(node.x, node.y, node.z);
          const size = radiusFor(entities[position]);
          dummy.scale.setScalar(size);
          dummy.updateMatrix();
          mesh.setMatrixAt(position, dummy.matrix);
        }
        mesh.instanceMatrix.needsUpdate = true;
        // The instances just moved, and InstancedMesh.raycast tests against a
        // bounding sphere three.js computes once, lazily, and never
        // invalidates. Left alone, a hover during the settle freezes that
        // sphere around the half-settled layout, and afterwards every pick
        // aimed at a node that drifted outside it silently misses — hover
        // highlight and click-to-select both stop working on the outer ring.
        mesh.computeBoundingSphere();

        for (const group of [typed, loose]) {
          group.subset.forEach((link, position) => {
            const from = nodes[link.fromIndex];
            const to = nodes[link.toIndex];
            if (!from || !to) return;
            group.positions.set(
              [from.x, from.y, from.z, to.x, to.y, to.z],
              position * 6,
            );
          });
          group.geometry.attributes.position.needsUpdate = true;
          group.geometry.computeBoundingSphere();
        }
        loose.lines.computeLineDistances();

        sprites.forEach((sprite) => {
          const position = index.get(sprite.userData.entityId as string);
          if (position === undefined) return;
          const node = nodes[position];
          // The point the label's bottom edge is pinned to: the top of the
          // sphere, plus a hair. The clearance itself comes from the sprite's
          // anchor, which is in screen space and so holds at any depth.
          sprite.position.set(node.x, node.y + node.radius + 1, node.z);
        });
      }

      /**
       * Hand out the screen to labels, most-mentioned first.
       *
       * Names are laid out by the graph, which knows nothing about how long
       * they are, so in any cluster they overlap into an unreadable heap — and
       * the heap is worst exactly where the graph is most interesting. So each
       * frame every label is projected to its pixel box and claims it if it can:
       * a label whose box overlaps one already claimed steps aside for this
       * frame and comes back when the camera moves it clear. Priority is
       * mentions, so the hub keeps its name and the passing mention yields.
       *
       * Per frame because it depends on the camera, and cheap enough to be:
       * MAX_LABELS is 40, so the worst case is ~800 rectangle tests.
       */
      function declutterLabels() {
        placements.length = 0;
        for (const sprite of sprites) {
          projected.copy(sprite.position).project(camera);
          placements.push({
            // NDC to pixels. The sprite is anchored by its bottom edge, so this
            // is the bottom-centre of the label, not its middle.
            anchorX: (projected.x * 0.5 + 0.5) * viewWidth,
            anchorY: (-projected.y * 0.5 + 0.5) * viewHeight,
            aspect: sprite.userData.aspect as number,
            // Behind the camera: three would billboard it back into view.
            behind: projected.z > 1,
          });
        }
        const shown = placeLabels(placements, viewWidth, viewHeight);
        sprites.forEach((sprite, position) => {
          sprite.visible = shown[position];
        });
      }

      function animate() {
        frame = requestAnimationFrame(animate);
        // The layout is already settled — the pre-settle above runs it to
        // convergence — so these ticks only matter for a graph that hit the tick
        // cap. A permanently hot simulation burns battery for no visual gain.
        if (settling > 0) {
          simulation.tick();
          settling -= 1;
          syncGeometry();
        }
        controls.update();
        // After controls.update(), which is what moved the camera.
        declutterLabels();
        renderer.render(scene, camera);
      }
      syncGeometry();
      applyHighlight();
      animate();

      const observer = new ResizeObserver(() => {
        const nextWidth = mount.clientWidth || width;
        const nextHeight = mount.clientHeight || height;
        viewWidth = nextWidth;
        viewHeight = nextHeight;
        camera.aspect = nextWidth / nextHeight;
        camera.updateProjectionMatrix();
        renderer.setSize(nextWidth, nextHeight);
        // Labels are a fixed number of pixels tall, so a shorter viewport is a
        // larger share of it. Without this they grew and shrank with the panel.
        applyLabelScale();
      });
      observer.observe(mount);

      // WebGL contexts are a finite resource; a lost one must not spin the loop.
      const onContextLost = (event: Event) => {
        event.preventDefault();
        cancelAnimationFrame(frame);
        setFailure("The WebGL context was lost. Reload to restore the graph.");
      };
      renderer.domElement.addEventListener("webglcontextlost", onContextLost);

      cleanup = () => {
        highlightRef.current = null;
        cancelAnimationFrame(frame);
        observer.disconnect();
        renderer.domElement.removeEventListener("pointermove", onPointerMove);
        renderer.domElement.removeEventListener("pointerleave", onPointerLeave);
        renderer.domElement.removeEventListener("click", onClick as EventListener);
        renderer.domElement.removeEventListener("webglcontextlost", onContextLost);
        controls.dispose();
        simulation.stop();
        geometry.dispose();
        material.dispose();
        mesh.dispose();
        for (const group of [typed, loose]) {
          group.geometry.dispose();
          group.material.dispose();
        }
        for (const sprite of sprites) sprite.material.dispose();
        for (const texture of labelTextures) texture.dispose();
        renderer.dispose();
        // forceContextLoss frees the GPU context immediately instead of waiting
        // for GC, which matters when the user flips between views repeatedly.
        renderer.forceContextLoss();
        if (renderer.domElement.parentNode === mount) {
          mount.removeChild(renderer.domElement);
        }
        void pointerInside;
      };
    })();

    return () => {
      disposed = true;
      cleanup?.();
    };
    // `signature` stands in for the entity and edge arrays the body reads
    // through refs: it is their content, where the props were only their
    // identity.
  }, [signature, theme]);

  if (entities.length === 0) {
    return (
      <div className={`graph-3d empty ${className ?? ""}`}>
        <p>No entities yet.</p>
      </div>
    );
  }

  return (
    <div className={`graph-3d ${className ?? ""}`}>
      <div ref={mountRef} className="graph-3d-canvas" />
      {failure ? (
        <div className="graph-3d-failure">{failure}</div>
      ) : null}
      <div className="graph-3d-legend">
        {entityLegend(
          theme,
          // The "other" swatch appears only while some node actually wears the
          // fallback colour — a row for nothing is noise.
          entities.some((entity) => !(entity.entity_type in PALETTES[theme].types)),
        ).map((entry) => (
          <span key={entry.type}>
            <i className="swatch node" style={{ background: entry.color }} /> {entry.label}
          </span>
        ))}
        <span>
          <i className="swatch typed" /> typed relation
        </span>
        <span>
          <i className="swatch loose" /> co-occurrence
        </span>
      </div>
    </div>
  );
}
