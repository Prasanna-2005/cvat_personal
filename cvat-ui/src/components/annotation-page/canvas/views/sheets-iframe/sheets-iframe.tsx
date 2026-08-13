// Copyright (C) Quantrium
//
// SPDX-License-Identifier: MIT

import './styles.scss';
import React from 'react';
import { useSelector } from 'react-redux';
import { CombinedState } from 'reducers';
import { shallowEqual } from 'utils/redux';

export function toSheetsEmbedUrl(url: string): string {
    try {
        const parsed = new URL(url);
        if (parsed.hostname.includes('docs.google.com') && parsed.pathname.includes('/spreadsheets/')) {
            parsed.searchParams.set('rm', 'minimal');
            return parsed.toString();
        }
    } catch (_error) {
        // fall through and return the original URL
    }
    return url;
}

function SheetsIframe(): JSX.Element {
    const { sheetsSideViewUrl } = useSelector((state: CombinedState) => ({
        sheetsSideViewUrl: state.annotation.sheetsSideViewUrl,
    }), shallowEqual);

    if (!sheetsSideViewUrl) {
        return <div className='cvat-sheets-iframe-empty'>No sheet selected</div>;
    }

    return (
        <iframe
            className='cvat-sheets-iframe'
            title='Google Sheet'
            src={toSheetsEmbedUrl(sheetsSideViewUrl)}
            allow='clipboard-read; clipboard-write'
            referrerPolicy='no-referrer-when-downgrade'
        />
    );
}

export default React.memo(SheetsIframe);
