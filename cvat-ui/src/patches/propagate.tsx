// Frontend-only "Propagate" interactor
// Propagates all annotation objects strictly inside the drawn bbox (ROI)
// to N frames forward, regardless of label.

import notification from 'antd/lib/notification';
import { ObjectState } from 'cvat-core-wrapper';
import { fetchAnnotationsAsync } from 'actions/annotation-actions';
import { getCVATStore } from 'cvat-store';
import {
    core, OcrPatchContext, getShapeAabb, strictAabbOverlap,
} from './patch-helpers';

export async function handlePropagateInteraction(
    ctx: OcrPatchContext,
    _interactor: any,
    _data: any,
    selBbox: [number, number, number, number] | undefined,
    frameCount: number,
): Promise<void> {
    const { states, dispatch, jobInstance, frame } = ctx.props;

    if (!selBbox) {
        notification.warning({
            message: 'Propagate: Please draw a bounding box to define the region.',
            duration: 3,
        });
        return;
    }

    if (!frameCount || frameCount < 1) {
        notification.warning({
            message: 'Propagate: Please enter a valid number of frames (≥ 1).',
            duration: 3,
        });
        return;
    }

    const [selXtl, selYtl, selXbr, selYbr] = selBbox;

    // Collect ALL annotation objects strictly inside the bbox (label-agnostic)
    const toPropagate: ObjectState[] = [];
    for (const state of states) {
        if (state.clientID == null) continue;
        if (state.hidden == true) continue;
        const aabb = getShapeAabb(state);
        if (!aabb) continue;

        const [xtl, ytl, xbr, ybr] = aabb;
        if (!strictAabbOverlap(xtl, ytl, xbr, ybr, selXtl, selYtl, selXbr, selYbr)) continue;

        toPropagate.push(state);
    }

    if (toPropagate.length === 0) {
        notification.info({
            message: 'Propagate: No annotations found strictly inside the selected region.',
            duration: 3,
        });
        return;
    }

    // Get frameNumbers from redux store (needed by propagateShapes)
    const store = getCVATStore();
    const reduxState = store.getState();
    const { frameNumbers } = reduxState.annotation.job;

    const toFrame = Math.min(frame + frameCount, jobInstance.stopFrame);

    try {
        let totalCreated = 0;
        for (const objectState of toPropagate) {
            const propagated = core.utils.propagateShapes<ObjectState>(
                [objectState], frame, toFrame, frameNumbers,
            );
            if (propagated.length) {
                await jobInstance.annotations.put(propagated);
                totalCreated += propagated.length;
            }
        }

        // Refresh annotations on canvas
        dispatch(fetchAnnotationsAsync());

        notification.success({
            message: `Propagate: ${toPropagate.length} object(s) propagated.`,
            duration: 4,
        });
    } catch (error: any) {
        notification.error({
            message: 'Propagate: Failed to propagate annotations.',
            description: error?.message || String(error),
            duration: null,
        });
    }
}
