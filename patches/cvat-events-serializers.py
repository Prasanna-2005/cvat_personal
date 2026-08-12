# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

import datetime
import json

from django.contrib.auth import get_user_model
from rest_framework import serializers

from cvat.apps.access_tokens.models import AccessToken

from .const import (
    GOOGLE_SHEETS_DELEGATION_USERNAME,
    GOOGLE_SHEETS_SOURCE,
    USER_ACTIVITY_SCOPE,
)


class EventSerializer(serializers.Serializer):
    scope = serializers.CharField(required=True)
    obj_name = serializers.CharField(required=False, allow_null=True)
    obj_id = serializers.IntegerField(required=False, allow_null=True)
    obj_val = serializers.CharField(required=False, allow_null=True)
    source = serializers.CharField(required=False, allow_null=True)
    timestamp = serializers.DateTimeField(required=True)
    count = serializers.IntegerField(required=False, allow_null=True)
    duration = serializers.IntegerField(required=False, default=0)
    project_id = serializers.IntegerField(required=False, allow_null=True)
    task_id = serializers.IntegerField(required=False, allow_null=True)
    job_id = serializers.IntegerField(required=False, allow_null=True)
    user_id = serializers.IntegerField(required=False, allow_null=True)
    user_name = serializers.CharField(required=False, allow_null=True)
    user_email = serializers.CharField(required=False, allow_null=True)
    org_id = serializers.IntegerField(required=False, allow_null=True)
    org_slug = serializers.CharField(required=False, allow_null=True)
    payload = serializers.CharField(required=False, allow_null=True)


class ClientEventsSerializer(serializers.Serializer):
    ALLOWED_SCOPES = {
        "client": frozenset(
            (
                "load:cvat",
                "load:job",
                "save:job",
                "load:workspace",
                "send:exception",
                "join:objects",
                "change:frame",
                "draw:object",
                "paste:object",
                "copy:object",
                "propagate:object",
                "drag:object",
                "resize:object",
                "delete:object",
                "merge:objects",
                "split:objects",
                "group:objects",
                "slice:object",
                "zoom:image",
                "fit:image",
                "rotate:image",
                "action:undo",
                "action:redo",
                "debug:info",
                "run:annotations_action",
                "click:element",
                USER_ACTIVITY_SCOPE,
                "call:interactor",
            )
        ),
        GOOGLE_SHEETS_SOURCE: frozenset((USER_ACTIVITY_SCOPE,)),
    }

    events = EventSerializer(many=True, default=[])
    previous_event = EventSerializer(default=None, allow_null=True, write_only=True)
    timestamp = serializers.DateTimeField()

    @staticmethod
    def _google_sheets_identity(request, event: dict, org) -> dict:
        if (
            request.user.username != GOOGLE_SHEETS_DELEGATION_USERNAME
            or not isinstance(getattr(request, "auth", None), AccessToken)
        ):
            raise serializers.ValidationError(
                {"source": "google_sheets events require the xyz AccessToken (PAT)"}
            )

        username = event.get("user_name")
        if not username:
            raise serializers.ValidationError(
                {"user_name": "user_name is required for google_sheets events"}
            )

        User = get_user_model()
        try:
            user = User.objects.get(username=username, is_active=True)
        except User.DoesNotExist as exc:
            raise serializers.ValidationError(
                {"user_name": f"Unknown or inactive user_name '{username}'"}
            ) from exc

        return {
            "org_id": getattr(org, "id", None),
            "org_slug": getattr(org, "slug", None),
            "user_id": user.id,
            "user_name": user.username,
            "user_email": user.email,
        }

    def to_internal_value(self, data):
        data = super().to_internal_value(data)
        request = self.context.get("request")
        org = request.iam_context["organization"]
        user_and_org_data = {
            "org_id": getattr(org, "id", None),
            "org_slug": getattr(org, "slug", None),
            "user_id": request.user.id,
            "user_name": request.user.username,
            "user_email": request.user.email,
        }

        send_time = data["timestamp"]
        receive_time = datetime.datetime.now(datetime.timezone.utc)
        time_correction = receive_time - send_time

        if data["previous_event"]:
            data["previous_event"]["timestamp"] += time_correction

        for event in data["events"]:
            scope = event["scope"]
            source = event.get("source", "client")
            if scope not in ClientEventsSerializer.ALLOWED_SCOPES.get(source, []):
                raise serializers.ValidationError(
                    {"scope": f"Event scope **{scope}** is not allowed from {source}"}
                )

            try:
                payload = json.loads(event.get("payload", "{}"))
            except json.JSONDecodeError:
                raise serializers.ValidationError(
                    {"payload": "JSON payload is not valid in passed event"}
                )

            if source == "client":
                identity = user_and_org_data
            elif source == GOOGLE_SHEETS_SOURCE:
                identity = self._google_sheets_identity(request, event, org)
            else:
                identity = {}

            event.update(
                {
                    "timestamp": event["timestamp"] + time_correction,
                    "source": source,
                    "payload": json.dumps(payload),
                    **identity,
                }
            )

        return data
