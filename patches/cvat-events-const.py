# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

import datetime

MAX_EVENT_DURATION = datetime.timedelta(seconds=100)
WORKING_TIME_RESOLUTION = datetime.timedelta(milliseconds=1)
WORKING_TIME_SCOPE = "send:working_time"
USER_ACTIVITY_SCOPE = "user:activity"
INTERACTOR_RESPONSE_SCOPE = "call:interactor"
IFRAME_OPEN_SCOPE = "interact:iframe:open"
IFRAME_CLOSE_SCOPE = "interact:iframe:close"
# Iframe close carries the full session duration. It is compressed only so
# get_end_timestamp() can recover the close instant; normal working-time
# contribution for this scope is handled separately and must not apply the
# inactivity cutoff.
COMPRESSED_EVENT_SCOPES = frozenset(("change:frame", IFRAME_CLOSE_SCOPE))
