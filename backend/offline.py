"""Explicit no-network Vapi substitute for complete offline rehearsals."""


class OfflineVapiClient:
    def __init__(self):
        self.said = []
        self.notes = []
        self.ended = []
        self.closed = False

    async def say(self, control_url, text):
        self.said.append((control_url, text))
        return True

    async def add_message(self, control_url, content, role="system", trigger_response=False):
        self.notes.append((control_url, content))
        return True

    async def get_call(self, call_id):
        return {"id": call_id, "monitor": {"controlUrl": f"offline://{call_id}"}}

    async def end_call(self, control_url):
        self.ended.append(control_url)
        return True

    async def create_call(self, *args, **kwargs):
        raise RuntimeError("Offline voice cannot place outbound calls; use SIM_UNITS=1")

    async def aclose(self):
        self.closed = True
