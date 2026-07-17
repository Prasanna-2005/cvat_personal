import {
    getCore, Job, Label, ObjectState, ShapeType,
} from 'cvat-core-wrapper';
import { ThunkDispatch } from 'utils/redux';

export const core = getCore();

interface OcrPatchContext {
    props: {
        curZOrder: number;
        createAnnotations: (states: ObjectState[]) => void;
        updateAnnotations: (states: ObjectState[]) => Promise<void>;
        fetchAnnotations: () => void;
        states: ObjectState[];
        labels: Label[];
        dispatch: ThunkDispatch;
        jobInstance: Job;
        frame: number;
    };
    state: {
        activeLabelID: number | null;
    };
}

function hasTextAttr(labelAttrs: { id?: number; name: string }[]): boolean {
    return labelAttrs.some((a) => a.name.toLowerCase().includes('text'));
}

function getTextAttrValue(state: ObjectState): string {
    const attrValues: Record<number, string> = state.attributes as Record<number, string>;
    const descriptors: { id?: number; name: string }[] = (state.label as any).attributes ?? [];
    for (const desc of descriptors) {
        if (desc.name.toLowerCase().includes('text') && desc.id !== undefined) {
            return attrValues[desc.id] ?? '';
        }
    }
    return '';
}

function getShapeAabb(
    state: ObjectState,
): [number, number, number, number] | null {
    const pts = state.points as number[];
    if (!pts || !pts.length) return null;

    if (state.shapeType === ShapeType.RECTANGLE) {
        return [pts[0], pts[1], pts[2], pts[3]];
    }

    if (state.shapeType === ShapeType.POLYGON) {
        let minX = Infinity; let minY = Infinity;
        let maxX = -Infinity; let maxY = -Infinity;
        for (let i = 0; i < pts.length - 1; i += 2) {
            const x = pts[i]; const y = pts[i + 1];
            if (x < minX) minX = x;
            if (y < minY) minY = y;
            if (x > maxX) maxX = x;
            if (y > maxY) maxY = y;
        }
        return [minX, minY, maxX, maxY];
    }

    return null;
}

function aabbOverlaps(
    xtl: number, ytl: number, xbr: number, ybr: number,
    selXtl: number, selYtl: number, selXbr: number, selYbr: number,
): boolean {
    if (xbr <= selXtl) return false;
    if (xtl >= selXbr) return false;
    if (ybr <= selYtl) return false;
    if (ytl >= selYbr) return false;
    return true;
}

function strictAabbOverlap(
    xtl: number, ytl: number, xbr: number, ybr: number,
    selXtl: number, selYtl: number, selXbr: number, selYbr: number,
): boolean {
    // Object must be fully contained within the selection
    return xtl >= selXtl && ytl >= selYtl && xbr <= selXbr && ybr <= selYbr;
}

export type { OcrPatchContext };
export {
    hasTextAttr,
    getTextAttrValue,
    getShapeAabb,
    aabbOverlaps,
    strictAabbOverlap,
}