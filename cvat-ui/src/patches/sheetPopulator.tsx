import notification from 'antd/lib/notification';
import { core, OcrPatchContext } from './patch-helpers';

// Parse interactor.description ("Task1/Task2/Task3") into selectable options
function parseSheetTasks(description: string | undefined): string[] {
    if (!description) return [];
    return description.split('/').map((task) => task.trim()).filter(Boolean);
}

async function handleSheetPopulatorInteraction(
    ctx: OcrPatchContext,
    interactor: any,
    data: any,
    selBbox: [number, number, number, number] | undefined,
    selectedTask: string | undefined,
): Promise<void> {
    const { jobInstance, labels, states, curZOrder, fetchAnnotations } = ctx.props;
    const { activeLabelID } = ctx.state;

    const labelInstance = labels.find((l: any) => l.id === activeLabelID);
    if (!labelInstance) {
        notification.error({ message: 'Sheet Populator Error', description: 'No active label selected.' });
        return;
    }

    const currentLabelName = labelInstance.name;
    const projectDataLabel = labels.find((label: any) => label.name.toLowerCase() === '_project_data');

    if (!projectDataLabel) {
        notification.error({
            message: 'Sheet Populator Error',
            description: 'No "_project_data" label found in this project.',
        });
        return;
    }

    const projectDataAttrs: any[] = projectDataLabel.attributes || [];
    const lowerLabelName = currentLabelName.toLowerCase();

    const driveFolderAttr = projectDataAttrs.find(
        (attr: any) => attr.name.toLowerCase() === `${lowerLabelName}_drive_folder`,
    );
    const templateAttr = projectDataAttrs.find(
        (attr: any) => attr.name.toLowerCase() === `${lowerLabelName}_template`,
    );

    if (!driveFolderAttr || !templateAttr) {
        notification.error({
            message: 'Sheet Populator Error',
            description: `Missing "_project_data" attrs: "${currentLabelName}_drive_folder" / "${currentLabelName}_template".`,
        });
        return;
    }

    const folderUrl = driveFolderAttr.defaultValue;
    const templateUrl = templateAttr.defaultValue;
    const frame = data.frame;
    const projectName = (jobInstance as any).projectName || 'Project';
    const taskId = jobInstance.taskId;

    const pointsPayload = selBbox || [];
    if (!pointsPayload || pointsPayload.length < 4) {
        throw new Error("Unable to determine spatial bounding box coordinates for object generation.");
    }

    const excelAttr = (labelInstance as any).attributes?.find(
        (attr: any) => attr.name.toLowerCase() === 'excel_link',
    );

    const object = new core.classes.ObjectState({
        frame,
        objectType: core.enums.ObjectType.SHAPE,
        source: core.enums.Source.SEMI_AUTO,
        label: labelInstance,
        shapeType: core.enums.ShapeType.RECTANGLE,
        points: pointsPayload,
        occluded: false,
        zOrder: curZOrder,
        attributes: {
            [excelAttr.id]: ""
        },
    });
    const [clientID] = await jobInstance.annotations.put([object]);
    fetchAnnotations();

    const newFileName = `${projectName}_${taskId}_${jobInstance.id}_${currentLabelName}${clientID}_frame${frame}`;

    try {
        const response = await core.lambda.call(jobInstance.taskId, interactor, {
            ...data,
            job: jobInstance.id,
            template_url: templateUrl,
            folder_url: folderUrl,
            new_file_name: newFileName,
            ai_task: selectedTask,
        }) as any;

        if (response && response.url) {
            // Re-fetch to get the latest server-backed states with proper save context
            const freshStates: any[] = await jobInstance.annotations.get(frame, false, []);
            const savedState = freshStates.find((s: any) => s.clientID === clientID);
            if (savedState && excelAttr) {
                savedState.attributes = { ...(savedState.attributes as Record<number, string>), [excelAttr.id]: response.url };
                await ctx.props.updateAnnotations([savedState]);
            }

            notification.success({
                message: 'Sheet Populated',
                description: `Sheet updated (${response.rows_updated ?? 0} row(s)) and linked successfully.`,
            });
        } else {
            notification.error({ message: 'Sheet Populator Error', description: 'No URL returned from sheet-populator.' });
        }
    } catch (error: any) {
        notification.error({
            message: 'Sheet Populator Error',
            description: `Failed to populate sheet: ${error?.message || error}`,
        });
    }
}

export { parseSheetTasks, handleSheetPopulatorInteraction };