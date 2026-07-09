import notification from 'antd/lib/notification';
import { ObjectState, ShapeType } from 'cvat-core-wrapper';
import {
    OcrPatchContext, getShapeAabb, strictAabbOverlap,
} from './patch-helpers';

type LabelKind = 'block' | 'line' | 'subblock' | null;

interface AttrDescriptor {
    id?: number;
    name: string;
}

interface Entry {
    state: ObjectState;
    aabb: [number, number, number, number];
    center: [number, number];
}

function classifyLabel(name: string | undefined): LabelKind {
    const normalized = (name ?? '').trim().toLowerCase();
    if (normalized === 'block') return 'block';
    if (normalized === 'line') return 'line';
    if (normalized === 'sub block' || normalized === 'subblock' || normalized === 'sub_block') return 'subblock';
    return null;
}

function getLabelAttrs(state: ObjectState): AttrDescriptor[] {
    return (state.label as any).attributes ?? [];
}

function findAttrId(labelAttrs: AttrDescriptor[], attrName: string): number | null {
    const target = attrName.trim().toLowerCase();
    const found = labelAttrs.find((a) => a.name.trim().toLowerCase() === target);
    return found && found.id !== undefined ? found.id : null;
}

function getAttrValue(state: ObjectState, attrId: number): string {
    const values = state.attributes as Record<number, string>;
    return values?.[attrId] ?? '';
}

function setAttrValue(state: ObjectState, attrId: number, value: string): void {
    state.attributes = {
        ...(state.attributes as Record<number, string>),
        [attrId]: value,
    };
}

// given a pos angle , performs rotation in anticlockwise and clockwise for neg angle
function rotatePoint(x: number, y: number, theta: number): [number, number] {
    const cos = Math.cos(theta);
    const sin = Math.sin(theta);
    return [x * cos - y * sin, x * sin + y * cos];
}

// Angle (radians) that the longest edge of a polygon makes with the x-axis. (+ve ang : 4th quad , -ve:first quad)
function longestEdgeAngle(pts: number[]): number {
    const vertexCount = Math.floor(pts.length / 2);
    let bestLenSq = -1;
    let theta = 0;

    for (let i = 0; i < vertexCount; i += 1) {
        const x1 = pts[i * 2];
        const y1 = pts[i * 2 + 1];
        const j = (i + 1) % vertexCount;
        const x2 = pts[j * 2];
        const y2 = pts[j * 2 + 1];

        const dx = x2 - x1;
        const dy = y2 - y1;
        const lenSq = (dx * dx) + (dy * dy);

        if (lenSq > bestLenSq) {
            bestLenSq = lenSq;
            theta = Math.atan2(dy, dx);
        }
    }

    return theta;
}

function effectiveGeometry(pts: number[], theta: number): { aabb: [number, number, number, number]; center: [number, number] } {
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    let sumX = 0;
    let sumY = 0;
    let count = 0;

    for (let i = 0; i + 1 < pts.length; i += 2) {
        const [rx, ry] = rotatePoint(pts[i], pts[i + 1], theta);
        if (rx < minX) minX = rx;
        if (rx > maxX) maxX = rx;
        if (ry < minY) minY = ry;
        if (ry > maxY) maxY = ry;
        sumX += rx;
        sumY += ry;
        count += 1;
    }

    return {
        aabb: [minX, minY, maxX, maxY],
        center: [sumX / count, sumY / count],
    };
}

interface PopulateResult {
    updated: ObjectState[];
    blockFound: boolean;
}

function populateNumbers(
    ctx: OcrPatchContext,
    selBbox: [number, number, number, number],
): PopulateResult {
    const { states } = ctx.props;
    const [selXtl, selYtl, selXbr, selYbr] = selBbox;

    let blockNo: string | null = null;
    let blockState: ObjectState | null = null;

    const lineStates: ObjectState[] = [];
    const subBlockStates: ObjectState[] = [];

    const pendingBlockNoTargets: ObjectState[] = [];

    // ---- Pass 1: filter the objects + propagate block_no --
    for (const state of states) {
        if (state.clientID == null) continue;

        const aabb = getShapeAabb(state);
        if (!aabb) continue;
        const [xtl, ytl, xbr, ybr] = aabb;

        if (!strictAabbOverlap(xtl, ytl, xbr, ybr, selXtl, selYtl, selXbr, selYbr)) continue;

        const kind = classifyLabel((state.label as any).name);
        const labelAttrs = getLabelAttrs(state);
        const blockNoAttrId = findAttrId(labelAttrs, 'block_no');

        if (kind === 'block') {
            blockState = state;
            blockNo = blockNoAttrId !== null ? getAttrValue(state, blockNoAttrId) : '';
            continue;
        }

        if (kind === 'line' || kind === 'subblock') {
            if (blockNoAttrId !== null) {
                if (blockNo !== null) {
                    setAttrValue(state, blockNoAttrId, blockNo);
                } else {
                    pendingBlockNoTargets.push(state);
                }
            }
            if (kind === 'line') {
                lineStates.push(state);
            } else {
                subBlockStates.push(state);
            }
        }
    }

    if (!blockState) {
        return { updated: [], blockFound: false };
    }

    // Backfill block_no on the (typically empty/small) set of objects seen before the Block.
    if (blockNo !== null && pendingBlockNoTargets.length) {
        for (const state of pendingBlockNoTargets) {
            const blockNoAttrId = findAttrId(getLabelAttrs(state), 'block_no');
            if (blockNoAttrId !== null) setAttrValue(state, blockNoAttrId, blockNo);
        }
    }

    // ---- Step 2: de-skew angle, derived once from the (unique) Block, 0 for plain rectangles ----
    const isPolygon = blockState.shapeType === ShapeType.POLYGON;
    const theta = isPolygon ? longestEdgeAngle(blockState.points as number[]) : 0;

    const lines: Entry[] = lineStates.map((state) => ({
        state,
        ...effectiveGeometry(state.points as number[], theta),
    }));
    const subBlocks: Entry[] = subBlockStates.map((state) => ({
        state,
        ...effectiveGeometry(state.points as number[], theta),
    }));

    // ---- Step 3: sort Lines top -> bottom (effective Y), assign line_no ----
    lines.sort((a, b) => a.aabb[1] - b.aabb[1]);
    lines.forEach((line, idx) => {
        const lineNoAttrId = findAttrId(getLabelAttrs(line.state), 'line_no');
        if (lineNoAttrId !== null) setAttrValue(line.state, lineNoAttrId, String(idx + 1));
    });

    // ---- Step 4: assign each Sub Block to the Line whose effective bbox contains its center ----
    const lineGroups: Entry[][] = lines.map(() => []);
    for (const sub of subBlocks) {
        const [cx, cy] = sub.center;
        let matchedIdx = -1;

        for (let i = 0; i < lines.length; i += 1) {
            const [lxtl, lytl, lxbr, lybr] = lines[i].aabb;
            if (cx >= lxtl && cx <= lxbr && cy >= lytl && cy <= lybr) {
                matchedIdx = i;
                break;
            }
        }

        if (matchedIdx === -1) continue; // no Lines in scope at all

        const lineNoAttrId = findAttrId(getLabelAttrs(sub.state), 'line_no');
        if (lineNoAttrId !== null) {
            setAttrValue(sub.state, lineNoAttrId, String(matchedIdx + 1));
        }
        lineGroups[matchedIdx].push(sub);
    }

    // ---- Step 5: populate sub_block_no, row by row
    for (const group of lineGroups) {
        if (!group.length) continue;
        group.sort((a, b) => a.aabb[0] - b.aabb[0]); // left -> right (effective X)

        group.forEach((sub, idx) => {
            const subNoAttrId = findAttrId(getLabelAttrs(sub.state), 'sub_block_no');
            if (subNoAttrId !== null) {
                setAttrValue(sub.state, subNoAttrId, idx % 2 === 0 ? '1' : '2');
            }
        });
    }

    const updated: ObjectState[] = [blockState, ...lines.map((l) => l.state), ...subBlocks.map((s) => s.state)];

    return { updated, blockFound: true };
}

async function handleNumberPopulatorInteraction(
    ctx: OcrPatchContext,
    _interactor: unknown,
    _data: unknown,
    selBbox: [number, number, number, number] | undefined,
): Promise<void> {
    const { updateAnnotations } = ctx.props;

    if (!selBbox) {
        notification.warning({ message: 'ID GENERATOR: no selection region found.', duration: 3 });
        return;
    }

    const { updated, blockFound } = populateNumbers(ctx, selBbox);

    if (!blockFound) {
        notification.warning({
            message: 'ID GENERATOR: no Block annotation found strictly inside the selected region.',
            duration: 3,
        });
    }

    if (updated.length) {
        await updateAnnotations(updated);
        notification.success({ message: `ID GENERATOR: updated ${updated.length} annotation(s).` });
    } else {
        notification.warning({ message: 'ID GENERATOR: no matching annotations found in selection.', duration: 3 });
    }
}

export {
    populateNumbers,
    handleNumberPopulatorInteraction,
};