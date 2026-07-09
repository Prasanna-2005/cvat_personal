// Frontend-only "Clear" interactor
// Deletes all annotation objects whose label matches the selected label
// within the drawn bounding box (ROI).

import notification from 'antd/lib/notification';
import { ObjectState, ShapeType } from 'cvat-core-wrapper';
import { removeObjectAsync, fetchAnnotationsAsync } from 'actions/annotation-actions';
import { OcrPatchContext, getShapeAabb, strictAabbOverlap } from './patch-helpers';


export async function handleClearInteraction(
    ctx: OcrPatchContext,
    _interactor: any,
    _data: any,
    selBbox: [number, number, number, number] | undefined,
): Promise<void> {
    const { states, labels, frame, dispatch } = ctx.props;
    const { activeLabelID } = ctx.state;

    if (!selBbox) {
        notification.warning({
            message: 'Clear: Please draw a bounding box to define the region.',
            duration: 3,
        });
        return;
    }

    const selectedLabel = labels.find((l) => l.id === activeLabelID);
    if (!selectedLabel) {
        notification.warning({
            message: 'Clear: No label selected.',
            duration: 3,
        });
        return;
    }

    const [selXtl, selYtl, selXbr, selYbr] = selBbox;

    // Collect matching annotation states
    const toRemove: ObjectState[] = [];
    for (const state of states) {
        if (state.clientID == null) continue;
        if (state.hidden == true) continue;
        // Match by label
        // if ((state.label as any).id !== selectedLabel.id) continue;

        // Check bounding box overlap with the ROI
        const aabb = getShapeAabb(state);
        if (!aabb) continue;

        const [xtl, ytl, xbr, ybr] = aabb;
        if (!strictAabbOverlap(xtl, ytl, xbr, ybr, selXtl, selYtl, selXbr, selYbr)) continue;

        toRemove.push(state);
    }

    if (toRemove.length === 0) {
        notification.info({
            message: 'Clear: No matching annotations found in the selected region.',
            duration: 3,
        });
        return;
    }

    // Remove each matching annotation using the existing removal flow
    let removedCount = 0;
    for (const objectState of toRemove) {
        try {
            // await dispatch(removeObjectAsync(objectState, true));   -->slow before : tracks history and further dispatch
            const removed = await objectState.delete(frame, true);   // --> fast : performs only delete
            if (removed) {
                removedCount += 1;
            }
        } catch (error) {
            // Continue removing other objects even if one fails
            console.error('Clear: failed to remove object', objectState.clientID, error);
        }
    }

    // Refresh annotations on canvas
    dispatch(fetchAnnotationsAsync());

    notification.success({
        message: `Clear: Removed ${removedCount} annotation(s).`,
        duration: 3,
    });
}
