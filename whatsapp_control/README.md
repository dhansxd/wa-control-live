# WhatsApp Control for Hermes Agent

This is the manual for the original standalone Control plugin. Use the
[repository setup guide](../README.md) to install the combined plugin. The
operation and chatbot policy reference below also applies to the combined service.

Manage separately paired WhatsApp accounts from Hermes and configure an optional chatbot per account. From the owner DM, select one or more account labels to send text or files to contacts or groups, publish text, photo, video, or voice Status, or create and administer groups. Guest replies use the chatbot's own instructions and recent context from the current chat. Guest model calls have no tools or owner memory.

This is a development candidate. Review the compatibility and phone test checklist before using it with production accounts. The plugin uses [Baileys](https://github.com/WhiskeySockets/Baileys), an unofficial WhatsApp Web library. WhatsApp may change or restrict this connection. Pair accounts you are authorized to manage and message people who expect to hear from you. [WhatsApp Messaging Guidelines](https://www.whatsapp.com/legal/messaging-guidelines)

## Install and pair

Use Hermes Agent 0.23.1 or later for natural-language owner commands from a WhatsApp DM. Hermes 0.22.7 or later can run the local CLI, service, and guest chatbot. Use a Hermes profile with `plugins.isolation: in_process`, Node.js 22.13 or later with npm, and one available Linked Devices slot on each account you plan to pair. [Hermes 0.23.1 sender context](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/gateway/session_context.py)

Install a reviewed GitHub commit using its full SHA. Replace `OWNER/REPO`, `FULL_COMMIT_SHA`, and the number placeholders with your own values. Accept the Node dependency prompt during installation. If the dependencies were skipped, run `npm ci` in the installed plugin directory. [Installing plugins in Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins#managing-plugins)

```sh
hermes plugins install OWNER/REPO --ref FULL_COMMIT_SHA --enable
hermes whatsapp-control setup --owner OWNER_NUMBER --command-account HERMES_NUMBER
hermes whatsapp-control serve
```

In bot mode, `--owner` is your number (B) and `--command-account` is the Hermes WhatsApp number that receives your commands (A). This excludes both sides of that conversation from automatic replies. Enter numbers in international format. In self-chat mode, you can omit `--command-account` if it is the same number. You can also pass `--owner-chat` with the exact owner DM ID. Use `hermes -p PROFILE` in each terminal if you use a named profile, and send commands to that profile's WhatsApp gateway. Keep `serve` running while you use the plugin.

When the same number runs the stock Hermes gateway and the guest chatbot, configure the gateway to admit only the owner and silently ignore other DMs. The guest chatbot applies its own allowlists. An open stock gateway can answer alongside the chatbot and admit guests to the standard Hermes agent. [Hermes WhatsApp access control](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/whatsapp)

From another local terminal, link each account you intend to manage. Use lowercase labels such as `shop` or `personal`.

```sh
hermes whatsapp-control pair shop
hermes whatsapp-control pair personal
hermes whatsapp-control status
```

Scan each terminal QR in **WhatsApp → Settings → Linked Devices → Link a Device** on the intended account. Pair A, B, or both, and repeat for any additional accounts. Each pairing creates a companion session for this plugin. Hermes Chat and any calling plugin keep their own sessions. WhatsApp's per-account device limit applies to the combined set. [WhatsApp Linked Devices](https://faq.whatsapp.com/378279804439436/)

Send commands from the configured owner DM. For example, “Post a text Status from shop and share it with these two contacts,” or “Make shop my default account and turn off its group replies.” Hermes checks the prepared action and resolved targets against the owner message from the current turn. It can ask for missing details. A request to send to all contacts uses the contacts synced for the selected accounts and shows the size of that selection. Group changes follow the account's WhatsApp admin permissions. Open a new Hermes conversation if the tool list has not refreshed.

Voice Status takes a selected local mono Ogg/Opus file and an audience. “All contacts” and group sends use explicit recipients resolved in the selected account. WhatsApp's native saved broadcast lists are outside that workflow. [Baileys Status support](https://github.com/WhiskeySockets/Baileys/blob/v7.0.0-rc14/README.md#broadcast-lists--stories)

“All contacts” includes contacts confirmed as saved by the companion's contact sync. Contacts recorded by older versions remain available for explicit selection and rejoin “all contacts” when a new named contact sync confirms them. Names seen only in chat history also remain available for explicit selection. The companion can receive an incomplete roster, and the current Baileys release does not expose a reliable contact-deletion event. Select recipients explicitly if the synced roster may be stale.

You can ask Hermes to forward a recent message from a named chat and account. The connected companion keeps a short in-memory window of messages available for forwarding and photo quotes. Message data from other accounts and chats cannot stand in for the selected original. View-once media is skipped.

Ask Hermes for the status of a previous operation or to retry it. The operation ID keeps the same accounts and destinations during retries. For a send, `accepted` means WhatsApp acknowledged the request at the server. Recipient visibility is checked separately. A retry skips accepted destinations and ambiguous actions, and resumes destinations that had definitely failed or never started. A repeated request with an unresolved receipt points back to the original operation. For an all-contact operation created before saved-contact verification, check the original receipt and select recipients in a new request. Group creation is settled only when WhatsApp returns the exact response for that creation request, including the group ID.

When one owner turn repeats a send to the same account and recipient, `duplicate` points to the original operation and shows its current receipt. Matching phone and LID addresses requires an unambiguous pair in the sending account's synced contacts.

While a send is unconfirmed, a later request with the same verified recipient and content points to the original operation. Unlinking one account removes its data from that operation. The operation ID still shows receipts for accounts that remain linked, and retries stay within those accounts and their originally selected recipients. If an older version already removed the plan, the surviving receipts can still be checked by operation ID.

If a device session stops working and must be linked again, run `hermes whatsapp-control repair shop` and scan the new QR from the same WhatsApp account. The plugin validates the identity before replacing the pairing and keeps the label, chatbot settings, and history. Remove an obsolete companion from Linked Devices if it is still listed.

## Chatbot behavior

DM and group replies start disabled. Set each account's instructions for a personal auto-reply, community FAQ, or store chatbot. DMs and groups can each be set to `off`, `allowlist`, or `all`. The chatbot excludes configured owner identities. Group replies can require a WhatsApp mention or respond to eligible messages directly.

In DMs, human handoff follows the sender's intent in ordinary language. It pauses that chat for the configured duration. The owner can reply manually and ask Hermes to show active pauses, extend one, or resume early. A new eligible message after expiration can receive a bot reply. Recorded voice notes can use configured transcription and synthesis commands. Image understanding requires a compatible Hermes model. [Hermes Plugin LLM API](https://hermes-agent.nousresearch.com/docs/developer-guide/plugin-llm-access)

Ask Hermes to change ordinary settings in natural language, or run `configure` while the worker is running. This example enables replies to one example number, keeps group replies off, and pauses for an hour on handoff.

```sh
hermes whatsapp-control configure shop '{"dm_mode":"allowlist","dm_allowlist":["15550100100"],"group_mode":"off","instructions":"Answer shop questions using the information I provide.","history_limit":0,"handoff_seconds":3600}'
```

Use group IDs ending in `@g.us` for `group_allowlist`. `require_mention` defaults to `true` and accepts a boolean or a map of group IDs to booleans. `exclude_chats` adds chats or senders to skip. `history_limit` sets the recent message count for the current account and chat, up to 100, with `0` disabling history. `handoff_seconds: 0` disables temporary handoff.

Set `image_enabled` to interpret supported JPEG or PNG attachments and `voice_enabled` to control voice note input. Choose `reply_mode` as `text`, `voice`, or `match` to mirror an incoming voice note. The default voice output uses the Hermes profile's TTS configuration. You can override it per account with a local `tts_command` that reads `{input_path}` and writes `{output_path}`. Voice input uses the configured `stt_command`, which reads `{input_path}` and prints the transcript to standard output. Set these argument arrays with the local CLI and use [the example policy](examples/chatbot-policy.json) as a starting point. Use mono Ogg/Opus output or install FFmpeg to convert supported audio. The chatbot falls back to text if synthesis fails or WhatsApp definitively rejects the voice note. [Hermes voice and TTS settings](https://hermes-agent.nousresearch.com/docs/user-guide/features/tts)

## Stop, disable, and remove

| Operation | Commands and effect |
| --- | --- |
| Stop the running worker | `hermes whatsapp-control stop` closes the plugin connections and retains their local sessions. |
| Disable the plugin | Run `hermes plugins disable hermes-whatsapp-control`. The worker detects the change and disconnects. Follow any gateway restart instruction Hermes prints. |
| Enable again | Run `hermes plugins enable hermes-whatsapp-control`, then `hermes whatsapp-control serve`. Existing valid sessions reconnect without a fresh QR. |
| Remove one linked account | While serving, run `hermes whatsapp-control unlink shop`. Verify removal on the phone if the unlink is unconfirmed. |
| Remove and retain local sessions | Run `hermes whatsapp-control cleanup`, then `hermes plugins remove hermes-whatsapp-control`. |
| Remove and purge local sessions | While serving, run `hermes whatsapp-control cleanup --purge`, then `hermes plugins remove hermes-whatsapp-control`. Purge attempts to unlink each companion, stops the worker, and deletes this plugin's runtime data. |

Run cleanup before disabling or removing the plugin, while its CLI is available. If you purge while the worker is stopped, or the result reports `remote_unlinked: false`, remove the matching companion from **Linked Devices** on that account. Native `remove` preserves the separate runtime data directory. [Hermes durable plugin state](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins#store-durable-state) · [Unlink a WhatsApp device](https://faq.whatsapp.com/834124628020911/?helpref=hc_fnav)

Hermes TTS runs in a cancellable child process using the worker's Hermes installation and profile. Stopping, disabling, or purging the worker cancels in-progress speech and removes the plugin's temporary audio before releasing its lock. If the speech child cannot start or validate its profile, eligible replies fall back to text.

To purge after removal, reinstall and enable the plugin in the same profile, start `serve`, complete cleanup, then remove the plugin again. Check Linked Devices on the handset afterward.

To install a newer reviewed commit over a pinned installation, use `hermes plugins install OWNER/REPO --force --ref NEW_FULL_COMMIT_SHA` after stopping the worker. Check readiness and reopen it with `serve`. [Hermes plugin management](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins#managing-plugins)

The first v0.2.0 start associates an existing transport database with the profile path. Start it in the original profile first. To use a copy at a different path, keep the original pairing with the original profile, purge the copied plugin data, then set up and link a new companion in the copy. Pending v0.1.0 sends have no stored WhatsApp receipt ID and must be checked in WhatsApp before being sent again.

## Data handling

Pairing creates WhatsApp credentials and Signal keys. Session state, account settings, handoff state, and configured chat history live under `plugin-data/hermes-whatsapp-control` in the Hermes profile. Keep that data, QR codes, and account IDs shown by `status` private.

Hermes sends the current owner request and the relevant prepared action to its configured model provider to check the action. When chatbot replies are enabled, Hermes sends the chatbot instructions and eligible conversation input to that provider. Image input can include the original attachment. Configured speech services may receive recordings or reply text. Use synthetic data when reporting failures.

## Verification before release

- Install a pinned candidate in an empty Hermes profile. Link two test accounts and verify owner-only routing, Status visibility on a recipient phone, one media send, and group membership changes.
- Test chatbot settings for eligible and ineligible DMs and groups. Verify temporary handoff, the first new reply after expiry, voice note playback, and answers that rely on an attached photo. Test cancellation during download, model execution, and speech synthesis.
- Run install, enable, disable, re-enable, update, and both removal paths during a real session. Verify process state, local files, and the correct companion on the handset.
- Check the exact Git commit, its history, and any release artifacts for QR codes, account data, credentials, message logs, and media. Use synthetic fixtures in CI.

## Hermes compatibility checks

The owner tool binds the incoming request and execution using the documented `turn_id` from `pre_llm_call` and `tool_execution`. Session identity comes through `get_session_env`. Read and write actions also require the current user row in the `pre_llm_call` history to have a `platform_message_id` and no synthetic `display_kind`. This uses the current event when Hermes retains the opening message's routing context for a queued turn. Hermes exposes both documented transcript fields in that hook on the versions tested below. Owner actions stop before contacting the worker if the evidence is absent. [Event hooks](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks#plugin-hooks) · [Transcript metadata](https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage#messages-table) · [Middleware](https://hermes-agent.nousresearch.com/docs/developer-guide/middleware)

Model calls use the plugin LLM API and voice replies use Hermes's registered `text_to_speech` tool. CI checks runtime imports against a pinned copy of the upstream public API manifest. [Public plugin API](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/public-api)

The `0.3.1` audit exercised owner commands with synthetic requests on Hermes v0.23.1 and upstream main at commit `422ad186`. The v0.23.1 validator warns about `agent.file_safety.raise_if_read_blocked`, which exists in that release and was added to the public API list afterward. The plugin uses that helper to apply Hermes's read policy before it handles an attachment. If a host does not provide the helper, attachment operations stop before reading the file. [Hermes attachment policy](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins#check-file-paths-and-read-yaml-with-hermess-own-helpers) · [Upstream API change](https://github.com/NousResearch/hermes-agent/commit/978889969477b49129b1bf285a1166fafb74b82b)

Both tested Hermes loaders can log `provides_middleware` as an unknown manifest field. Hermes's validator requires that declaration, and both loaders register and execute the middleware through `register(ctx)`. [Manifest loader](https://github.com/NousResearch/hermes-agent/blob/422ad1861856dd9b80b947fbad9f39d1580f6b41/hermes_cli/plugins_manifest.py) · [Plugin validator](https://github.com/NousResearch/hermes-agent/blob/422ad1861856dd9b80b947fbad9f39d1580f6b41/hermes_cli/plugin_validate.py)

The lockfile includes GPL-3.0 `libsignal` and LGPL-3.0-or-later `libvips` dependencies installed by npm. The repository ships source and a lockfile. Dependency distributions keep their own license terms.

The code in this repository is licensed under the [MIT License](LICENSE). Dependencies and their transitive dependencies retain their own licenses.
