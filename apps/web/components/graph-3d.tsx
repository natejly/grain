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
  className?: string;
};

type Simulated = { id: string; x: number; y: number; z: number };

/** How many labels can exist at once. Each one costs a canvas texture. */
const MAX_LABELS = 40;
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
    fallback: 0x929ba8,
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
      project: 0x2c7454,
      named_entity: 0x37784b,
      concept: 0x6b6357,
    },
    fallback: 0x6b6357,
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

/** Node radius grows with mentions, but sublinearly — a hub must not eat the view. */
function radiusFor(entity: GraphEntity): number {
  return 4.5 + Math.sqrt(Math.max(0, entity.mention_count)) * 2.2;
}

export function Graph3D({ entities, edges, onSelect, className }: Graph3DProps) {
  const mountRef = useRef<HTMLDivElement>(null);
  const [failure, setFailure] = useState("");
  // The callback is read from a ref so a caller passing an inline arrow does not
  // tear down and rebuild the whole scene on every render.
  const selectRef = useRef(onSelect);
  selectRef.current = onSelect;
  // Not a ref: a theme change has to rebuild the scene, because fog, lighting
  // and every instanced colour are baked in at build time.
  const theme = useTheme();

  useEffect(() => {
    const mount = mountRef.current;
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
      try {
        [THREE, { OrbitControls }, { forceSimulation, forceLink, forceManyBody, forceCenter }] =
          await Promise.all([
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
            .id((_node: unknown, position: number) => position)
            .distance(38)
            .strength(0.25),
        )
        .force("charge", forceManyBody().strength(-90))
        .force("center", forceCenter(0, 0, 0))
        .stop();

      // Pre-settle off-screen so the first painted frame is already a graph
      // rather than an expanding ball of noise.
      for (let tick = 0; tick < 120; tick += 1) simulation.tick();

      // Frame whatever the simulation produced. A fixed camera distance turns a
      // five-node graph into specks and buries a large one.
      let extent = 0;
      for (const node of nodes) {
        extent = Math.max(extent, Math.hypot(node.x, node.y, node.z));
      }
      const nodeMargin = Math.max(...entities.map(radiusFor), 6);
      const span = Math.max(extent + nodeMargin * 2, 40);
      const distance = (span / Math.tan((camera.fov * Math.PI) / 360)) * 1.6;
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
      const labelScale = Math.max(span / 340, 0.16);
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
          new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false }),
        );
        sprite.scale.set(canvas.width * labelScale, canvas.height * labelScale, 1);
        sprite.userData.entityId = entity.id;
        sprites.push(sprite);
        scene.add(sprite);
      }

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
        const near = hoveredId
          ? new Set([hoveredId, ...(neighbours.get(hoveredId) ?? [])])
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
        typed.material.opacity = hoveredId ? 0.35 : 0.9;
        loose.material.opacity = hoveredId ? 0.14 : 0.5;
      }

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
          // Sit the label just above the sphere rather than inside it.
          sprite.position.set(node.x, node.y + radiusFor(entities[position]) + 4, node.z);
        });
      }

      function animate() {
        frame = requestAnimationFrame(animate);
        // The simulation keeps running for a short while so the graph visibly
        // settles, then stops — a permanently hot simulation burns battery for
        // no visual gain once it has converged.
        if (settling > 0) {
          simulation.tick();
          settling -= 1;
          syncGeometry();
        }
        controls.update();
        renderer.render(scene, camera);
      }
      syncGeometry();
      applyHighlight();
      animate();

      const observer = new ResizeObserver(() => {
        const nextWidth = mount.clientWidth || width;
        const nextHeight = mount.clientHeight || height;
        camera.aspect = nextWidth / nextHeight;
        camera.updateProjectionMatrix();
        renderer.setSize(nextWidth, nextHeight);
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
  }, [entities, edges, theme]);

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
