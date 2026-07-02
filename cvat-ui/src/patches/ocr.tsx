import notification from 'antd/lib/notification';
import { InteractorResults, ObjectState } from 'cvat-core-wrapper';
import {
    core, OcrPatchContext, hasTextAttr, getShapeAabb, aabbOverlaps,
} from './patch-helpers';

//------------- PATCH - 1 ------------------------------------------------
//------- OCR: construct a shape and an OCR interactor response for one cell

async function constructFromOCR(
    ctx: OcrPatchContext,
    response: any,
    labelInstance: any,
    frame: number,
): Promise<void> {
    const { curZOrder, createAnnotations } = ctx.props;
    const { text, bbox } = response;

    // 1. Locate the correct spec_id for the attribute named "text"
    const textAttribute = labelInstance.attributes.find((attr: any) => attr.name.toLowerCase().includes('text'));
    if (!textAttribute) {
        throw new Error(`Label "${labelInstance.name}" is missing a target attribute named "text".`);
    }

    // 2. Unpack rectangle bounds from Nuclio payload: [x1, y1, x2, y2]
    const points = [
        bbox[0][0], // left
        bbox[0][1], // top
        bbox[1][0], // right
        bbox[1][1], // bottom
    ];

    // 3. Build canonical ObjectState using browser-layer attribute format
    const object = new core.classes.ObjectState({
        frame,
        objectType: core.enums.ObjectType.SHAPE,
        source: core.enums.Source.SEMI_AUTO,
        label: labelInstance,
        shapeType: core.enums.ShapeType.RECTANGLE,
        points,
        occluded: false,
        zOrder: curZOrder,
        attributes: {
            [textAttribute.id]: text.trim(),
        },
    });

    // 4. Dispatch through CVAT's native Redux flow
    createAnnotations([object]);
}

async function handleOcrInteraction(
    ctx: OcrPatchContext,
    interactor: any,
    data: any,
): Promise<void> {
    const { jobInstance } = ctx.props;
    const { activeLabelID } = ctx.state;

    const response = await core.lambda.call(jobInstance.taskId, interactor, {
        ...data,
        job: jobInstance.id,
    }) as InteractorResults & Record<string, unknown>;

    const labelInstance = jobInstance.labels.find((l: any) => l.id === activeLabelID);

    if (labelInstance) {
        await constructFromOCR(ctx, response, labelInstance, data.frame);
    }
}


// ------------- PATCH - 3 : OCR for skewed docs------------------
//  OCR: build/apply payloads for the skew-correction OCR lambda (range odf cells)

function buildOcrPayload(
    ctx: OcrPatchContext,
    selBbox: [number, number, number, number],
): {
    bboxes: Record<string, number[]>;
    cellStateMap: Record<number, ObjectState>;
} {
    const { states, labels } = ctx.props;
    const { activeLabelID } = ctx.state;

    const selectedLabel = labels.find((l) => l.id === activeLabelID);
    const [selXtl, selYtl, selXbr, selYbr] = selBbox;

    const bboxes: Record<string, number[]> = {};
    const cellStateMap: Record<number, ObjectState> = {};

    for (const state of states) {
        if (state.clientID == null) continue;
        if (!selectedLabel || (state.label as any).id !== selectedLabel.id) continue;

        const labelAttrs: { id?: number; name: string }[] = (state.label as any).attributes ?? [];
        if (!hasTextAttr(labelAttrs)) continue;

        const pts = state.points as number[];
        if (!pts || pts.length < 3) continue;

        const aabb = getShapeAabb(state);
        if (!aabb) continue;

        const [xtl, ytl, xbr, ybr] = aabb;
        if (!aabbOverlaps(xtl, ytl, xbr, ybr, selXtl, selYtl, selXbr, selYbr)) continue;

        // Send all raw points — flexible for both polygon and rectangle
        // Rectangle: [x1,y1,x2,y2] → 4 values
        // Polygon:   [x1,y1,x2,y2,...,xn,yn] → 2n values
        bboxes[String(state.clientID)] = pts;
        cellStateMap[state.clientID] = state;
    }

    return { bboxes, cellStateMap };
}

async function applyOcrResults(
    ctx: OcrPatchContext,
    lambdaResponse: Record<string, string>,
    cellStateMap: Record<number, ObjectState>,
): Promise<void> {
    const { updateAnnotations } = ctx.props;
    const updatedStates: ObjectState[] = [];

    console.log(lambdaResponse);

    for (const [cellIdStr, ocrText] of Object.entries(lambdaResponse)) {
        const cellId = Number(cellIdStr);
        const state = cellStateMap[cellId];
        if (!state || !ocrText) continue;

        const labelAttrs: { id?: number; name: string }[] = (state.label as any).attributes ?? [];
        const textAttr = labelAttrs.find((a) => a.name.toLowerCase().includes('text'));
        if (!textAttr || textAttr.id == null) continue;

        state.attributes = {
            ...(state.attributes as Record<number, string>),
            [textAttr.id]: ocrText,
        };
        updatedStates.push(state);
    }

    if (updatedStates.length) {
        await updateAnnotations(updatedStates);
        notification.success({
            message: `OCR: updated ${updatedStates.length} annotation(s).`,
        });
    }
}

async function handleSkewOcrInteraction(
    ctx: OcrPatchContext,
    interactor: any,
    data: any,
    selBbox: [number, number, number, number] | undefined,
): Promise<void> {
    const { jobInstance } = ctx.props;
    let bboxes: Record<string, number[]> = {};
    let cellStateMap: Record<number, ObjectState> = {};

    if (selBbox) {
        ({ bboxes, cellStateMap } = buildOcrPayload(ctx, selBbox));
    }

    if (!selBbox || Object.keys(bboxes).length === 0) {
        notification.warning({ message: 'OCR: no matching annotations found in selection.', duration: 3 });
        return;
    }

    const response = await core.lambda.call(jobInstance.taskId, interactor, {
        ...data,
        job: jobInstance.id,
        bboxes,
    }) as unknown as Record<string, string>;

    await applyOcrResults(ctx, response, cellStateMap);
}

// ---------------------------------------
export type {OcrPatchContext};
export {
    constructFromOCR,
    buildOcrPayload,
    applyOcrResults,
    handleOcrInteraction,
    handleSkewOcrInteraction
};