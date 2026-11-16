---
name: call-now
description: Use when the WhatsApp owner asks the assistant in a DM to call now or at a later time.
---

# Call the WhatsApp owner

Interpret the owner's latest DM in context, whether they typed it or sent a
voice note. If they are asking to be called now, call `whatsapp_call_now`
with empty arguments. If Hermes loads the tool on demand, use `tool_call` with
`{"calls":[{"name":"whatsapp_call_now","arguments":{}}]}`.

If the owner asks for one call at a future time, use the same tool with
`schedule` set to an ISO 8601 date and time in their timezone or a relative
delay such as `in 10m`. The tool creates the job through Hermes cron. Confirm
the time from `scheduled_at` after a `scheduled` result. Interpret the request
in context as you would an immediate call.

For `preparing_audio`, say that the request was accepted and audio is being
prepared. For `dial_requested`, confirm that the call request was accepted.
Ringing and answer require confirmation from the call status. A `denied`
result means the plugin did not start a call. A `busy` result means a call is
already in progress. The current reply goes to the WhatsApp DM. Use the tool
once for each request.
