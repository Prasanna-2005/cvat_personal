// Copyright (C) CVAT.ai Corporation
//
// SPDX-License-Identifier: MIT

import React from 'react';
import notification from 'antd/lib/notification';
import { getCore, ObjectState, ShapeType } from 'cvat-core-wrapper';
import { CombinedState, Workspace } from 'reducers';
import ObjectItemDetails, { SizeType } from 'components/annotation-page/standard-workspace/objects-side-bar/object-item-details';
import { updateAnnotationsAsync, collapseObjectItems } from 'actions/annotation-actions';
import { connect } from 'react-redux';
import { ThunkDispatch } from 'utils/redux';

const core = getCore();

interface OwnProps {
    readonly: boolean;
    clientID: number;
    parentID: number | null;
}

interface StateToProps {
    collapsed: boolean;
    state: ObjectState | null;
    workspace: Workspace;
    textContent: string;
    jobInstance: any;
    labels: any[];
}

interface DispatchToProps {
    updateState(objectState: any): void;
    collapseOrExpand(objectState: any, collapsed: boolean): void;
}

function mapStateToProps(state: CombinedState, own: OwnProps): StateToProps {
    const { clientID, parentID } = own;
    let objectState: ObjectState | null = null;
    const { states } = state.annotation.annotations;
    if (parentID) {
        const parentState = (states as ObjectState[])
            .find((_objectState: ObjectState) => _objectState.clientID === parentID);
        if (parentState) {
            objectState = parentState.elements.find((el: ObjectState) => el.clientID === clientID) || null;
        }
    } else {
        objectState = (states as ObjectState[]).find((el: ObjectState) => el.clientID === clientID) || null;
    }

    const {
        annotation: {
            annotations: {
                collapsedAll,
                collapsed: statesCollapsed,
            },
            job: { instance: jobInstance, labels },
            workspace,
        },
        settings: {
            workspace: {
                textContent,
            },
        },
    } = state;

    const collapsed = typeof statesCollapsed[clientID as number] === 'undefined' ? collapsedAll : statesCollapsed[clientID];

    return {
        collapsed,
        state: objectState,
        workspace,
        textContent,
        jobInstance,
        labels
    };
}

function mapDispatchToProps(dispatch: ThunkDispatch): DispatchToProps {
    return {
        updateState(state: ObjectState): void {
            dispatch(updateAnnotationsAsync([state]));
        },
        collapseOrExpand(objectState: ObjectState, collapsed: boolean): void {
            dispatch(collapseObjectItems([objectState], collapsed));
        },
    };
}

type Props = StateToProps & DispatchToProps & OwnProps;

class ObjectItemDetailsContainer extends React.PureComponent<Props> {
    private changeAttribute = (id: number, value: string): void => {
        const { state, readonly, updateState } = this.props;
        if (!readonly && state) {
            const attr: Record<number, string> = {};
            attr[id] = value;
            state.attributes = attr;
            updateState(state);
        }
    };

    private handleCreateExcel = async (attrID: number): Promise<void> => {
        const {
            state, jobInstance, labels
        } = this.props;

        if (!state || !jobInstance) return;

        // 1. Derive the current label name from the annotation's label
        const currentLabelName = state.label.name;

        // 2. Locate the _project_data label from all project labels
        const projectDataLabel = labels.find(
            (label: any) => label.name.toLowerCase() === '_project_data',
        );

        const fn = state.frame

        if (!projectDataLabel) {
            notification.error({
                message: 'Excel Link Error',
                description: 'No "_project_data" label found in this project. Please create one with the required configuration attributes.',
            });
            return;
        }

        // 3. Scan _project_data attributes for {labelName}_drive_folder and {labelName}_template
        //    using case-insensitive matching on both sides
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
                message: 'Excel Link Error',
                description: `Missing configuration in "_project_data" label. Expected attributes: "${currentLabelName}_drive_folder" and "${currentLabelName}_template".`,
            });
            return;
        }

        // 4. Read config values from the attribute defaultValue
        const folderUrl = driveFolderAttr.defaultValue;
        const templateUrl = templateAttr.defaultValue;

        if (!folderUrl || !templateUrl) {
            notification.error({
                message: 'Excel Link Error',
                description: `Configuration attributes for "${currentLabelName}" have empty default values. Please set the Google Drive folder URL and template URL.`,
            });
            return;
        }

        // 5. Build the file name: {projectName}_{taskId}_{jobId}_{labelName}_{frameNumber}
        const projectName = jobInstance.projectName || 'Project';
        const taskId = jobInstance.taskId || 'Task';
        const jobId = jobInstance.id;
        const objectId = state.clientID;

        const newFileName = `${projectName}_${taskId}_${jobId}_${currentLabelName}${objectId}_frame${fn}`;

        const { models: functions } = await core.lambda.list();
        const interactor = functions.find((f: any) => f.id === 'sheet-populator');
        if (!interactor) {
            throw new Error("Sheet Populator interactor not found in core.lambda.list()");
        }
        // 6. Call the sheet populator Nuclio function via CVAT's lambda API
        try {
            const result = await core.lambda.call(
                jobInstance.taskId,
                { id: 'sheet-populator' } as any,
                {
                    template_url: templateUrl,
                    folder_url: folderUrl,
                    new_file_name: newFileName,
                    frame: fn,
                    pos_points: [],
                    neg_points: [],
                    job: jobInstance.id,
                },
            );

            const response = result as any;

            if (response && response.url) {
                // 7. Persist the URL to the excel_link attribute
                this.changeAttribute(attrID, response.url);
                notification.success({
                    message: 'Excel Sheet Created',
                    description: 'Google Sheet has been created and linked successfully.',
                });
            } else {
                notification.error({
                    message: 'Excel Link Error',
                    description: 'The template duplicator did not return a valid URL.',
                });
            }
        } catch (error: any) {
            notification.error({
                message: 'Excel Link Error',
                description: `Failed to create Google Sheet: ${error?.message || error}`,
            });
        }
    };

    private changeSize = (type: SizeType, value: number): void => {
        const { state, readonly, updateState } = this.props;
        if (!readonly && state) {
            if (state.shapeType === ShapeType.CUBOID && state.points) {
                const points = state.points.slice();
                switch (type) {
                    case SizeType.LENGTH:
                        points[6] = value;
                        break;
                    case SizeType.WIDTH:
                        points[7] = value;
                        break;
                    case SizeType.HEIGHT:
                        points[8] = value;
                        break;
                    default:
                        break;
                }
                state.points = points;
            }
            updateState(state);
        }
    };

    private collapse = (): void => {
        const { state, collapseOrExpand, collapsed } = this.props;
        collapseOrExpand(state, !collapsed);
    };

    public render(): JSX.Element | null {
        const {
            readonly, collapsed, state, workspace, textContent,
        } = this.props;

        if (state) {
            let sizeParams = null;

            if (state.shapeType === ShapeType.CUBOID && workspace === Workspace.STANDARD3D && state.points) {
                sizeParams = {
                    length: parseFloat(state.points[6].toFixed(2)), // X
                    width: parseFloat(state.points[7].toFixed(2)), // Y
                    height: parseFloat(state.points[8].toFixed(2)), // Z
                };
            }
            return (
                <ObjectItemDetails
                    readonly={readonly}
                    collapsed={collapsed}
                    collapse={this.collapse}
                    changeAttribute={this.changeAttribute}
                    onCreateExcel={this.handleCreateExcel}
                    values={{ ...state.attributes }}
                    attributes={[...state.label.attributes]}
                    changeSize={this.changeSize}
                    sizeParams={sizeParams}
                    source={state.source}
                    score={state.score}
                    votes={state.votes}
                    textContent={textContent}
                />
            );
        }

        return null;
    }
}

export default connect<StateToProps, DispatchToProps, OwnProps, CombinedState>(
    mapStateToProps,
    mapDispatchToProps,
)(ObjectItemDetailsContainer);
