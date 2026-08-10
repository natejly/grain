/**
 * d3-force-3d ships no type declarations. Rather than `declare module` (which
 * makes the whole simulation `any` and hides real mistakes), this covers just
 * the surface graph-3d.tsx uses, typed accurately enough that a wrong argument
 * still fails the build.
 */
declare module "d3-force-3d" {
  export interface SimulationNode {
    x: number;
    y: number;
    z: number;
    vx?: number;
    vy?: number;
    vz?: number;
    fx?: number | null;
    fy?: number | null;
    fz?: number | null;
  }

  export interface Force {
    (alpha: number): void;
  }

  export interface Simulation<Node extends SimulationNode> {
    tick(iterations?: number): this;
    stop(): this;
    restart(): this;
    alpha(): number;
    alpha(value: number): this;
    alphaTarget(value: number): this;
    nodes(): Node[];
    nodes(nodes: Node[]): this;
    force(name: string, force: unknown): this;
    force(name: string): unknown;
  }

  export interface LinkForce<Link> {
    (alpha: number): void;
    id(accessor: (node: unknown, index: number) => number | string): this;
    distance(value: number | ((link: Link) => number)): this;
    strength(value: number | ((link: Link) => number)): this;
    links(): Link[];
    links(links: Link[]): this;
  }

  export interface ManyBodyForce {
    (alpha: number): void;
    strength(value: number | ((node: unknown) => number)): this;
    distanceMax(value: number): this;
    theta(value: number): this;
  }

  export interface CenterForce {
    (alpha: number): void;
    strength(value: number): this;
  }

  /** `numDimensions` is the 3D part: pass 3 for x/y/z. */
  export function forceSimulation<Node extends SimulationNode>(
    nodes?: Node[],
    numDimensions?: number,
  ): Simulation<Node>;

  export function forceLink<Link>(links?: Link[]): LinkForce<Link>;
  export function forceManyBody(): ManyBodyForce;
  export function forceCenter(x?: number, y?: number, z?: number): CenterForce;
}
