"""Thin return-type for WhatsAppClient.send_template_message — the Cloud API's real send
response has more fields (contacts[], messaging_product, a per-message message_status), none of
which this integration currently needs beyond the message id used to correlate later status
webhooks.
"""

from pydantic import BaseModel


class WhatsAppSendResult(BaseModel):
    message_id: str  # Meta's "wamid.<...>" — correlates inbound status webhooks
