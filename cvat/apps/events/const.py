# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

import datetime

MAX_EVENT_DURATION = datetime.timedelta(seconds=100)
# Google Sheets sessions include PDF/GT reading; allow longer idle gaps than the UI logger.
GOOGLE_SHEETS_MAX_EVENT_DURATION = datetime.timedelta(seconds=600)
WORKING_TIME_RESOLUTION = datetime.timedelta(milliseconds=1)
WORKING_TIME_SCOPE = "send:working_time"
USER_ACTIVITY_SCOPE = "user:activity"
INTERACTOR_RESPONSE_SCOPE = "call:interactor"
COMPRESSED_EVENT_SCOPES = frozenset(("change:frame",))

GOOGLE_SHEETS_SOURCE = "google_sheets"
# Only this PAT owner may submit delegated google_sheets events for other users.
GOOGLE_SHEETS_DELEGATION_USERNAME = "service_account"
