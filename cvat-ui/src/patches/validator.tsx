import notification from 'antd/lib/notification';
import { ObjectState } from 'cvat-core-wrapper';
import { reviewActions } from 'actions/review-actions';
import {
    core, OcrPatchContext, hasTextAttr, getTextAttrValue, getShapeAabb, aabbOverlaps,
} from './patch-helpers';



export function buildValidatorPayload(
    ctx: OcrPatchContext,
    selBbox: [number, number, number, number],
): {
    xDataRects: Record<string, { text: string; rects: number[] }>;
    cellStateMap: Record<number, ObjectState>;
} {
    const { states, labels } = ctx.props;
    const { activeLabelID } = ctx.state;

    const selectedLabel = labels.find((l) => l.id === activeLabelID);
    const [selXtl, selYtl, selXbr, selYbr] = selBbox;
    const xDataRects: Record<string, { text: string; rects: number[] }> = {};
    const cellStateMap: Record<number, ObjectState> = {};

    for (const state of states) {
        if (state.clientID == null) continue;
        if (!selectedLabel || (state.label as any).id !== selectedLabel.id) continue;

        const labelAttrs: { id?: number; name: string }[] = (state.label as any).attributes ?? [];
        if (!hasTextAttr(labelAttrs)) continue;

        const textop = getTextAttrValue(state);

        const aabb = getShapeAabb(state);
        if (!aabb) continue;

        const [xtl, ytl, xbr, ybr] = aabb;
        if (!aabbOverlaps(xtl, ytl, xbr, ybr, selXtl, selYtl, selXbr, selYbr)) continue;

        const pts = state.points as number[];

        const cellId = state.clientID;
        xDataRects[String(cellId)] = {
            text: textop,
            rects: pts,
        };

        cellStateMap[cellId] = state;
    }

    return { xDataRects, cellStateMap };
}



export async function dispatchValidatorIssues(
    ctx: OcrPatchContext,
    lambdaResponse: Record<string, Record<any, any>>,
    cellStateMap: Record<number, ObjectState>,
): Promise<void> {
    const { dispatch, jobInstance, frame } = ctx.props;

    let issueCount = 0;
    for (const [cellIdStr, result] of Object.entries(lambdaResponse)) {
        if (result.match) continue;

        const cellId = Number(cellIdStr);
        const state = cellStateMap[cellId];
        if (!state) continue;

        const aabb = getShapeAabb(state);
        if (!aabb) continue;

        const [xtl, ytl, xbr, ybr] = aabb;
        const position: number[] = [
            xtl, ytl,
            xbr, ytl,
            xbr, ybr,
            xtl, ybr,
            xtl, ytl,
        ];
        try {
            const issue = new core.classes.Issue({
                job: jobInstance.id,
                frame,
                position,
            });

            const lltext = result.lltext ?? '';
            const anntext = getTextAttrValue(state);

            const savedIssue = await jobInstance.openIssue(
            issue,
            `ISSUE-DIFF: ${JSON.stringify({ annotator: anntext, llm: lltext })}`
        );
            dispatch(reviewActions.finishIssueSuccess(frame, savedIssue));
            issueCount += 1;
        } catch (error) {
            dispatch(reviewActions.finishIssueFailed(error));
        }
    }
    if (issueCount === 0) {
        notification.success({
            message: 'Validator: No issues created.',
        });
    }
    if (issueCount > 0) {
        notification.success({
            message: `Validator: ${issueCount} issue(s) created.`,
        });
    }
}



export async function handleValidatorInteraction(
    ctx: OcrPatchContext,
    interactor: any,
    data: any,
    selBbox: [number, number, number, number] | undefined,
): Promise<void> {
    const { jobInstance } = ctx.props;
    let xDataRects: Record<string, { text: string; rects: number[] }> = {};
    let cellStateMap: Record<number, ObjectState> = {};

    if (selBbox) {
        ({ xDataRects, cellStateMap } = buildValidatorPayload(ctx, selBbox));
    }

    if (!selBbox || Object.keys(xDataRects).length === 0) {
        notification.warning({ message: 'Validator: no matching annotations found in selection.', duration: 3 });
        return;
    }

    const response = await core.lambda.call(jobInstance.taskId, interactor, {
        ...data,
        job: jobInstance.id,
        rects: xDataRects,
    }) as Record<string, unknown>;

    await dispatchValidatorIssues(ctx, response as Record<string, Record<any, any>>, cellStateMap);
}