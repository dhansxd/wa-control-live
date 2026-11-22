use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::Serialize;
use tokio::sync::{mpsc, watch};
use whatsapp_rust::prelude::*;
use whatsapp_rust::wacore::types::lid_pn::LearningSource;

use crate::calls::Bridge;

const RETAINED_MESSAGES: usize = 256;
const PAGE_SIZE: usize = 32;
const POLL_WAIT: Duration = Duration::from_secs(15);

#[derive(Clone, Serialize)]
pub struct OwnerMessage {
    id: String,
    chat_id: String,
    sender_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    sender_id_alt: Option<String>,
    text: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    media: Option<&'static str>,
}

#[derive(Serialize)]
pub struct MessagePage {
    cursor: String,
    messages: Vec<OwnerMessage>,
}

struct Retained {
    epoch: u64,
    last: u64,
    messages: VecDeque<(u64, OwnerMessage)>,
}

pub struct OwnerInbox {
    retained: Mutex<Retained>,
    changed: watch::Sender<()>,
}

impl OwnerInbox {
    pub fn new() -> Self {
        Self {
            retained: Mutex::new(Retained {
                epoch: rand::random(),
                last: 0,
                messages: VecDeque::with_capacity(RETAINED_MESSAGES),
            }),
            changed: watch::channel(()).0,
        }
    }

    fn insert(&self, message: OwnerMessage) {
        let mut retained = self.retained.lock().unwrap();
        if retained
            .messages
            .iter()
            .any(|(_, previous)| previous.id == message.id)
        {
            return;
        }
        if retained.messages.len() == RETAINED_MESSAGES {
            retained.messages.pop_front();
        }
        retained.last += 1;
        let seq = retained.last;
        retained.messages.push_back((seq, message));
        self.changed.send_replace(());
    }

    fn mark_ingress_gap(&self) {
        self.retained.lock().unwrap().epoch = rand::random();
        self.changed.send_replace(());
    }

    fn page(&self, cursor: Option<&str>) -> Result<MessagePage, ()> {
        let retained = self.retained.lock().unwrap();
        let earliest = retained
            .messages
            .front()
            .map_or(retained.last, |(seq, _)| seq - 1);
        let seq = match cursor {
            None => earliest,
            Some(cursor) => {
                let (epoch, seq) = cursor.split_once(':').ok_or(())?;
                let epoch = u64::from_str_radix(epoch, 16).map_err(|_| ())?;
                let seq = seq.parse::<u64>().map_err(|_| ())?;
                if epoch != retained.epoch || !(earliest..=retained.last).contains(&seq) {
                    return Err(());
                }
                seq
            }
        };
        let next = retained
            .messages
            .iter()
            .filter(|(index, _)| *index > seq)
            .take(PAGE_SIZE);
        let mut last = seq;
        let messages = next
            .map(|(index, message)| {
                last = *index;
                message.clone()
            })
            .collect();
        Ok(MessagePage {
            cursor: format!("{:016x}:{last}", retained.epoch),
            messages,
        })
    }

    pub async fn poll(&self, cursor: Option<&str>) -> Result<MessagePage, ()> {
        let mut changed = self.changed.subscribe();
        let page = self.page(cursor)?;
        if cursor.is_none() || !page.messages.is_empty() {
            return Ok(page);
        }
        let _ = tokio::time::timeout(POLL_WAIT, changed.changed()).await;
        self.page(cursor)
    }
}

pub fn selected_bot(client: &Client, expected: &str) -> bool {
    client
        .pn()
        .is_some_and(|pn| pn.is_pn() && pn.user == expected)
}

pub fn account_ready(bridge: &Bridge) -> bool {
    bridge.client.as_ref().is_some_and(|client| {
        client.is_logged_in()
            && bridge
                .cfg
                .expected_bot
                .as_deref()
                .is_none_or(|bot| selected_bot(client, bot))
    })
}

pub async fn owner_address(client: &Client, jid: &Jid, owner: &str) -> bool {
    if jid.is_pn() {
        return jid.user == owner;
    }
    if !jid.is_lid() {
        return false;
    }
    client
        .get_lid_pn_entry(jid)
        .await
        .ok()
        .flatten()
        .is_some_and(|entry| {
            entry.lid.as_ref() == jid.user.as_str()
            && entry.phone_number.as_ref() == owner
            // The peer fields here come from the authenticated WhatsApp stanza.
            // Historic/bulk hints alone cannot authorize this address.
            && matches!(entry.learning_source,
                LearningSource::Usync | LearningSource::RecipientLatestLid |
                LearningSource::DeviceNotification | LearningSource::PeerPnMessage |
                LearningSource::PeerLidMessage)
        })
}

pub struct OwnerMessageObserver {
    pub bridge: Arc<Bridge>,
    pub pending: mpsc::Sender<InboundMessage>,
}

impl EventHandler for OwnerMessageObserver {
    fn handle_event(&self, event: Arc<Event>) {
        if !account_ready(&self.bridge) {
            return;
        }
        for message in event.messages() {
            if owner_candidate(message, &self.bridge.cfg.owner).is_none() {
                continue;
            }
            if self.pending.try_send(message.clone()).is_err() {
                self.bridge.owner_inbox.as_ref().unwrap().mark_ingress_gap();
            }
        }
    }
}

fn owner_content(message: &wa::Message) -> (&str, Option<&'static str>) {
    if message.is_view_once() {
        return ("", None);
    }
    let message = message.get_base_message();
    if let Some(image) = message.image_message.as_option() {
        return (image.caption.as_deref().unwrap_or_default(), Some("image"));
    }
    if message
        .audio_message
        .as_option()
        .is_some_and(|audio| audio.ptt == Some(true))
    {
        return ("", Some("voice"));
    }
    (message.text_content().unwrap_or_default(), None)
}

fn owner_candidate(message: &InboundMessage, owner: &str) -> Option<Jid> {
    let info = &message.info;
    let source = &info.source;
    let (text, media) = owner_content(&message.message);
    if source.is_from_me
        || source.is_group
        || info.is_offline
        || info.unavailable_request_id.is_some()
        || info.id.is_empty()
        || (text.trim().is_empty() && media.is_none())
    {
        return None;
    }
    let chat = source.chat.to_non_ad();
    if chat != source.sender.to_non_ad()
        || !(chat.is_pn() || chat.is_lid())
        || (chat.is_pn() && chat.user != owner)
        || (chat.is_lid()
            && source
                .sender_alt
                .as_ref()
                .is_some_and(|alt| !alt.is_pn() || alt.user != owner))
    {
        return None;
    }
    Some(chat)
}

// A single worker preserves the event ordering across asynchronous mapping lookups.
pub async fn retain_owner_messages(
    bridge: Arc<Bridge>,
    mut pending: mpsc::Receiver<InboundMessage>,
) {
    let (Some(client), Some(inbox)) = (&bridge.client, &bridge.owner_inbox) else {
        return;
    };
    while let Some(message) = pending.recv().await {
        let Some(chat) = owner_candidate(&message, &bridge.cfg.owner) else {
            continue;
        };
        if !owner_address(client, &chat, &bridge.cfg.owner).await || !account_ready(&bridge) {
            continue;
        }
        let wire = chat.to_string();
        let (text, media) = owner_content(&message.message);
        inbox.insert(OwnerMessage {
            id: message.info.id.to_string(),
            chat_id: wire.clone(),
            sender_id: wire,
            sender_id_alt: chat
                .is_lid()
                .then(|| Jid::pn(bridge.cfg.owner.as_str()).to_string()),
            text: text.to_owned(),
            media,
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use whatsapp_rust::wacore::types::message::MessageSource;

    fn message(id: usize) -> OwnerMessage {
        OwnerMessage {
            id: id.to_string(),
            chat_id: "15551110000@s.whatsapp.net".into(),
            sender_id: "15551110000@s.whatsapp.net".into(),
            sender_id_alt: None,
            text: "hello".into(),
            media: None,
        }
    }

    fn live_dm(chat: Jid, sender: Jid, alternate: Option<Jid>) -> MessageInfo {
        MessageInfo {
            source: MessageSource {
                chat,
                sender,
                sender_alt: alternate,
                ..Default::default()
            },
            id: "live-message".into(),
            ..Default::default()
        }
    }

    fn candidate(info: MessageInfo) -> Option<Jid> {
        let incoming = InboundMessage::builder()
            .message(Arc::new(wa::Message::text("hello")))
            .info(Arc::new(info))
            .build();
        owner_candidate(&incoming, "15551110000")
    }

    #[test]
    fn owner_candidate_keeps_wire_address_and_discards_non_live_or_unrelated_dms() {
        let owner = Jid::pn("15551110000");
        let lid = Jid::lid("99995551110000");
        let stranger = Jid::pn("15552220000");
        assert_eq!(
            candidate(live_dm(owner.clone(), owner.with_device(2), None)),
            Some(owner.clone())
        );
        assert_eq!(
            candidate(live_dm(lid.clone(), lid.clone(), Some(owner.clone()))),
            Some(lid.clone()),
        );
        assert!(candidate(live_dm(lid.clone(), lid, Some(stranger.clone()))).is_none());
        assert!(
            candidate(live_dm(
                stranger.clone(),
                stranger.clone(),
                Some(owner.clone())
            ))
            .is_none()
        );
        assert!(candidate(live_dm(owner.clone(), stranger, None)).is_none());
        let mut offline = live_dm(owner.clone(), owner.clone(), None);
        offline.is_offline = true;
        assert!(candidate(offline).is_none());
        let mut echo = live_dm(owner.clone(), owner, None);
        echo.source.is_from_me = true;
        assert!(candidate(echo).is_none());
    }

    #[test]
    fn replays_only_new_rows_and_detects_lag_and_restarts() {
        let inbox = OwnerInbox::new();
        let beginning = inbox.page(None).unwrap().cursor;
        inbox.insert(message(1));
        inbox.insert(message(1));
        let page = inbox.page(Some(&beginning)).unwrap();
        assert_eq!(page.messages.len(), 1);
        assert_eq!(page.messages[0].id, "1");
        assert!(inbox.page(Some(&page.cursor)).unwrap().messages.is_empty());
        assert!(OwnerInbox::new().page(Some(&page.cursor)).is_err());
        for id in 2..=RETAINED_MESSAGES + 2 {
            inbox.insert(message(id));
        }
        assert!(inbox.page(Some(&page.cursor)).is_err());
        let retained = inbox.page(None).unwrap();
        assert_eq!(retained.messages.len(), PAGE_SIZE);
        assert_eq!(retained.messages[0].id, "3");
        inbox.mark_ingress_gap();
        assert!(inbox.page(Some(&retained.cursor)).is_err());
    }

    #[tokio::test]
    async fn polling_wakes_on_insert() {
        let inbox = OwnerInbox::new();
        let cursor = inbox.page(None).unwrap().cursor;
        let (reading, ()) = tokio::join!(inbox.poll(Some(&cursor)), async {
            tokio::task::yield_now().await;
            inbox.insert(message(1));
        });
        assert_eq!(reading.unwrap().messages[0].id, "1");
    }
}
