# Changelog

## Unreleased

- Require an actual inbound owner message for tool reads and writes, including when an internal queued turn inherits the opening DM's routing context.
- Preserve receipt status and the remaining retry scope when one account is unlinked. Invalidate prepared work and stop pending chatbot replies for that account so a reused label cannot inherit them.
- Check unresolved sends across owner turns using the sending account's verified recipient aliases. Recover surviving receipt status from older operations whose plan was removed.
- Cancel native Hermes speech and clean its temporary audio when stopping, disabling, or purging the worker. The speech child verifies the same Hermes installation, plugin, and profile before dispatching the configured TTS tool.
- Limit all-contact sends and Status to synced contacts confirmed by the bridge. Keep history-only contacts available for explicit lookup and preserve receipts from older all-contact operations.
- Reclaim media when a canceled download returns late. Serialize local purge with worker startup, report saved accounts as offline after interruptions, and recheck that a selected attachment is a regular file before hashing it.

## 0.3.1 development candidate

- Bind owner actions to the authenticated Hermes turn, including consecutive messages in the same DM when the gateway reuses its session task ID.
- Reuse the original send receipt for the same content, sending account, and destination in one turn, including matched phone and LID pairs from synced contacts. Include same-name contacts and groups in the information used to verify destinations.
- Use the documented Hermes attachment policy and check public API imports in CI.

## 0.3.0 development candidate

- Use the Hermes profile's speech synthesis settings for voice replies, with the existing per-account command override.
- Compose Voice Status from mono Ogg/Opus and a verified audience using the same send receipts as other Status media.
- Track group creation by its exact WhatsApp response, and prevent a new request from repeating the same action while an earlier result is unresolved.
- Add a sample chatbot policy and credential scanning in CI.

## 0.2.0 development candidate

- Verify outbound actions and settings against the authenticated owner's current request. Freeze the selected accounts, recipients and attachment content before dispatch.
- Distinguish accepted, rejected and unconfirmed results. Reuse the operation ID to retry definite failures without repeating accepted destinations. The worker can reconcile send receipts and some visible group changes.
- Add a default sending account, same-account repair, recent message forwarding, and commands for viewing and extending chatbot pauses.
- Match verified phone and LID aliases in guest policy. Add recent quoted images, an independent voice input toggle, text fallback for definitively rejected voice notes, and recovery of temporary media after a crash.
- Protect migrated pairing data from being opened at a copied profile path. Add offline CI and upgrade coverage for existing receipts.

## 0.1.0 development candidate

- Add separate QR pairing per account, owner controls, Status, group administration, configurable chatbot replies and native Hermes plugin lifecycle commands.
