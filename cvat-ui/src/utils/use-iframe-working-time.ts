// Copyright (C) Quantrium
//
// SPDX-License-Identifier: MIT

import { useLayoutEffect, useRef } from 'react';
import { useSelector } from 'react-redux';
import { Event, Job } from 'cvat-core-wrapper';
import { EventScope } from 'cvat-logger';
import { CombinedState } from 'reducers';

function createIframeSessionId(): string {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
        return crypto.randomUUID();
    }
    return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
}

/**
 * Observes the existing sheets iframe state (`sheetsSideViewUrl`) and emits
 * iframe working-time events on open/close transitions.
 *
 * Close is started with wait=true at open time so the close event keeps the
 * original open timestamp and receives duration on finalize.
 */
export default function useIframeWorkingTime(job: Job | null | undefined): void {
    const sheetsSideViewUrl = useSelector((state: CombinedState) => state.annotation.sheetsSideViewUrl);
    const pendingCloseRef = useRef<Event | null>(null);
    const urlRef = useRef<string | null>(null);
    const jobRef = useRef(job);
    const chainRef = useRef(Promise.resolve());
    jobRef.current = job;

    const endSession = (): Promise<void> => {
        const currentJob = jobRef.current;
        if (currentJob) {
            currentJob.iframeSessionId = null;
        }
        const pending = pendingCloseRef.current;
        pendingCloseRef.current = null;
        if (!pending) {
            return Promise.resolve();
        }
        return pending.close().then(() => undefined);
    };

    const startSession = async (): Promise<void> => {
        const currentJob = jobRef.current;
        if (!currentJob) {
            return;
        }
        const iframeSessionId = createIframeSessionId();
        currentJob.iframeSessionId = iframeSessionId;
        const payload = {
            iframe_session_id: iframeSessionId,
            source: 'iframe',
        };
        const closeEvent = await currentJob.logger.log(EventScope.interactIframeClose, payload, true);
        if (currentJob.iframeSessionId !== iframeSessionId) {
            await closeEvent.close();
            return;
        }
        pendingCloseRef.current = closeEvent;
        await currentJob.logger.log(EventScope.interactIframeOpen, payload);
    };

    useLayoutEffect(() => {
        if (!job) {
            if (urlRef.current) {
                urlRef.current = null;
                chainRef.current = chainRef.current.then(() => endSession()).catch((error: unknown) => {
                    console.error('Failed to record iframe working-time events', error);
                });
            }
            return;
        }

        const previousUrl = urlRef.current;
        const nextUrl = sheetsSideViewUrl;
        if (previousUrl === nextUrl) {
            return;
        }
        urlRef.current = nextUrl;

        chainRef.current = chainRef.current
            .then(async () => {
                if (previousUrl) {
                    await endSession();
                }
                if (nextUrl) {
                    await startSession();
                }
            })
            .catch((error: unknown) => {
                console.error('Failed to record iframe working-time events', error);
            });
    }, [sheetsSideViewUrl, job]);

    useLayoutEffect(() => () => {
        const pending = pendingCloseRef.current;
        const currentJob = jobRef.current;
        pendingCloseRef.current = null;
        if (currentJob) {
            currentJob.iframeSessionId = null;
        }
        if (pending) {
            pending.close().catch((error: unknown) => {
                console.error('Failed to close iframe working-time session', error);
            });
        }
    }, []);
}
