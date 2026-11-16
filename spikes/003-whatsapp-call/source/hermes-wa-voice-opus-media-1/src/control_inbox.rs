use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tokio::sync::watch;
use whatsapp_rust::prelude::*;
use whatsapp_rust::wacore::chrono::Utc;
use whatsapp_rust::wacore_binary::JidExt;

use crate::calls::Bridge;
use crate::owner_chat::account_ready;

const CAPACITY: usize = 200;
const MESSAGE_AGE: Duration = Duration::from_secs(600);
const POLL_WAIT: Duration = Duration::from_secs(15);

pub struct ControlInbox {
    retained: Mutex<Retained>,
    changed: watch::Sender<()>,
}

struct Retained {
    epoch: u64,
    last: u64,
    messages: VecDeque<(u64, Instant, InboundMessage)>,
}

impl ControlInbox {
    pub fn new() -> Self {
        Self {
            retained: Mutex::new(Retained {
                epoch: rand::random(),
                last: 0,
                messages: VecDeque::with_capacity(CAPACITY),
            }),
            changed: watch::channel(()).0,
        }
    }

    fn insert(&self, message: InboundMessage) {
        let info = &message.info;
        let chat = &info.source.chat;
        if !(chat.is_group() || chat.is_pn() || chat.is_lid())
            || info.id.is_empty()
            || (info.is_offline && !info.source.is_from_me)
            || info.unavailable_request_id.is_some()
            || crate::server::control::forwardable(&message.message).is_none()
        {
            return;
        }
        let mut retained = self.retained.lock().unwrap();
        if retained.messages.iter().any(|(_, _, previous)| {
            previous.info.id == info.id && previous.info.source.chat == *chat
        }) {
            return;
        }
        if retained.messages.len() == CAPACITY {
            retained.messages.pop_front();
        }
        retained.last += 1;
        let index = retained.last;
        retained
            .messages
            .push_back((index, Instant::now(), message));
        self.changed.send_replace(());
    }

    pub fn remember_sent(&self, chat: Jid, sender: Jid, sent: SendResult) {
        let mut info = MessageInfo {
            id: sent.message_id.as_str().into(),
            timestamp: Utc::now(),
            ..Default::default()
        };
        info.source.is_from_me = true;
        info.source.is_group = chat.is_group();
        info.source.chat = chat;
        info.source.sender = sender.to_non_ad();
        self.insert(
            InboundMessage::builder()
                .info(Arc::new(info))
                .message(sent.message)
                .build(),
        );
    }

    pub fn find(&self, chat: &Jid, id: &str) -> Option<InboundMessage> {
        self.retained
            .lock()
            .unwrap()
            .messages
            .iter()
            .rev()
            .find_map(|(_, seen, message)| {
                (seen.elapsed() <= MESSAGE_AGE
                    && message.info.id.as_str() == id
                    && message.info.source.chat.to_non_ad() == *chat)
                    .then(|| message.clone())
            })
    }

    pub fn recent(&self, chat: &Jid) -> Vec<InboundMessage> {
        self.retained
            .lock()
            .unwrap()
            .messages
            .iter()
            .rev()
            .filter(|(_, seen, message)| {
                seen.elapsed() <= MESSAGE_AGE && message.info.source.chat.to_non_ad() == *chat
            })
            .map(|(_, _, message)| message.clone())
            .collect()
    }

    fn page(&self, cursor: Option<&str>) -> Result<(String, Vec<InboundMessage>), ()> {
        let retained = self.retained.lock().unwrap();
        let earliest = retained
            .messages
            .front()
            .map_or(retained.last, |(seq, _, _)| seq - 1);
        let mut last = match cursor {
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
        let after = last;
        let mut messages = Vec::new();
        for (index, seen, message) in retained
            .messages
            .iter()
            .filter(|(seq, _, _)| *seq > after)
            .take(32)
        {
            if seen.elapsed() <= MESSAGE_AGE {
                messages.push(message.clone());
            }
            last = *index;
        }
        Ok((format!("{:016x}:{last}", retained.epoch), messages))
    }

    pub async fn poll(&self, cursor: Option<&str>) -> Result<(String, Vec<InboundMessage>), ()> {
        let mut changed = self.changed.subscribe();
        let page = self.page(cursor)?;
        if cursor.is_none() || !page.1.is_empty() || Some(page.0.as_str()) != cursor {
            return Ok(page);
        }
        let _ = tokio::time::timeout(POLL_WAIT, changed.changed()).await;
        self.page(cursor)
    }
}

pub struct ControlObserver(pub Arc<Bridge>);

impl EventHandler for ControlObserver {
    fn handle_event(&self, event: Arc<Event>) {
        if account_ready(&self.0)
            && let Some(inbox) = &self.0.control_inbox
        {
            for message in event.messages() {
                inbox.insert(message.clone());
            }
        }
    }
}
