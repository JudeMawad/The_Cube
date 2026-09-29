"""Cube-level self controls. All hardware work belongs to the client."""
from .channel import ClientUnavailable


class CubeCommands:
    def __init__(self, channel):
        self.channel = channel

    def execute(self, context, operation, **arguments):
        if not context.control_session or not context.request_id:
            return {"success": False, "error": "cube_unavailable"}
        try:
            result = self.channel.request(context.client_id, context.control_session,
                                          context.request_id, operation, **arguments)
        except ClientUnavailable:
            return {"success": False, "error": "cube_unavailable"}
        facts = {"success": result.success}
        if result.error:
            facts["error"] = result.error
        if result.status:
            facts["status"] = result.status.model_dump(exclude_none=True)
        if result.volume:
            facts["volume"] = result.volume.model_dump(exclude_none=True)
        if result.success and operation != "get_status":
            facts["accepted"] = True
        return facts
